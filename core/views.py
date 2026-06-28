import json
import secrets
from datetime import date
from functools import wraps
from django.conf import settings as dj_settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError
from django.shortcuts import render, redirect
from django.http import JsonResponse, FileResponse, Http404
from django.contrib.auth import authenticate, login, logout
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .models import (
    User, Booking, CreditTransaction, Receipt, AvailabilityOverride,
    CustomTime, StudentNote, ActiveLesson, SiteSettings, LessonFile
)

# Stripe is an optional dependency: the app must import and run without it (the
# tutor-mediated purchase flow is always available). When the package is missing
# or no secret key is configured, the Stripe endpoints report "disabled".
try:
    import stripe
except ImportError:  # pragma: no cover - exercised only where stripe isn't installed
    stripe = None

# Uploaded lesson materials: an allowed type, reasonably sized.
MAX_LESSON_FILE_BYTES = 25 * 1024 * 1024  # 25 MB
AUDIO_EXTS = {"mp3", "m4a", "wav", "ogg"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
DOC_EXTS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "rtf"}
ALLOWED_LESSON_EXTS = AUDIO_EXTS | IMAGE_EXTS | DOC_EXTS | {"zip"}


def file_ext(name):
    return name.rsplit(".", 1)[-1].lower() if name and "." in name else ""


def file_kind(name):
    e = file_ext(name)
    if e in AUDIO_EXTS:
        return "audio"
    if e in IMAGE_EXTS:
        return "image"
    if e in DOC_EXTS:
        return "doc"
    return "file"


# ---------------------------------------------------------------------------
# JS date-key helpers
#
# The frontend builds date keys with JS `Date.getMonth()`, which is 0-indexed
# (January = 0). Python's `date.month` is 1-indexed. These helpers translate
# between a real Python date and the JS "YYYY-M-D" key so DB dates stay
# semantically correct while still round-tripping with the client.
# ---------------------------------------------------------------------------
def date_to_jskey(d):
    return f"{d.year}-{d.month - 1}-{d.day}"


def jskey_to_date(key):
    year, month_idx, day = (int(p) for p in key.split("-"))
    return date(year, month_idx + 1, day)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def compute_initials(name):
    """First letters of the first two words, e.g. 'Jan Heissenberger' -> 'JH'."""
    parts = [p for p in (name or "").strip().split() if p]
    if not parts:
        return "NS"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def serialize_user(u):
    return {
        "id": u.slug,
        "slug": u.slug,
        "name": u.get_full_name() or u.username,
        "email": u.email,
        "initials": u.initials,
        "credits": u.credits,
        "color1": u.color1,
        "color2": u.color2,
        "photo": u.photo,
        "role": u.role,
        "billing": {
            "name": u.billing_name,
            "line1": u.billing_line1,
            "postcode": u.billing_postcode,
            "city": u.billing_city,
            "country": u.billing_country,
        },
    }


def booking_student_slug(b):
    return b.student.slug if b.student_id and b.student else b.student_slug


def booking_tutor_slug(b):
    return b.tutor.slug if b.tutor_id and b.tutor else b.tutor_slug


def serialize_booking(b):
    return {
        "pk": b.pk,
        "studentId": booking_student_slug(b),
        "tutorId": booking_tutor_slug(b),
        "date": b.date.isoformat(),
        "time": b.time,
        "title": b.title,
        "notes": b.notes,
        "tutorNotes": b.tutor_notes,
        "callLink": b.call_link,
        # Guest "intro" bookings have no student account; the tutor UI shows the
        # guest's name/e-mail from here instead of looking them up in the roster.
        "isIntro": b.is_intro,
        "guestName": b.guest_name,
        "guestEmail": b.guest_email,
        "guestPhone": b.guest_phone,
    }


def blocker_booking(b):
    """Anonymized booking sent to a student so the calendar blocks the slot
    without revealing who booked it or any session details."""
    return {
        "pk": None,
        "studentId": "__blocked__",
        "tutorId": booking_tutor_slug(b),
        "date": b.date.isoformat(),
        "time": b.time,
        "title": "",
        "notes": "",
        "tutorNotes": "",
        "callLink": "",
    }


def serialize_transaction(t):
    # Map integer amount to string "+N" / "-N" / ""
    if t.amount > 0:
        amt_str = f"+{t.amount}"
    elif t.amount < 0:
        amt_str = str(t.amount)
    else:
        amt_str = ""
    return {
        "type": t.txn_type,
        "label": t.label,
        "sub": t.sub,
        "amt": amt_str,
        "receiptNo": t.receipt_no or None,
    }


def serialize_receipt(r):
    # Read from the frozen snapshot, not the live user: the receipt must read the
    # same forever, and the student may no longer exist.
    return {
        "no": r.number,
        "dateStr": r.date_str,
        "studentId": r.student_slug,
        "studentName": r.student_name,
        "billing": {
            "name": r.billing_name,
            "line1": r.billing_line1,
            "postcode": r.billing_postcode,
            "city": r.billing_city,
            "country": r.billing_country,
        },
        "credits": r.credits,
        "unit": r.unit_price_cents,  # already in EUR (stored as integer EUR)
        "net": r.credits * r.unit_price_cents,
        "total": r.credits * r.unit_price_cents,
    }


def serialize_lesson_file(lf):
    return {
        "id": lf.pk,
        "lessonId": lf.lesson_id,
        "name": lf.original_name,
        "ext": file_ext(lf.original_name).upper() or "FILE",
        "kind": file_kind(lf.original_name),
        "url": f"/api/lesson-files/download/{lf.pk}/",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_settings():
    return SiteSettings.objects.first() or SiteSettings.objects.create()


def parse_price(s):
    """Numeric euros from a price string like '€270' / '270,50' (None if absent)."""
    import re
    m = re.search(r"\d+(?:[.,]\d+)?", str(s or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def receipt_unit_price(settings, n):
    """Per-credit price for a purchase of n credits. If n matches a configured
    pack, use that pack's total ÷ n so the receipt reflects the package price
    (e.g. 10 credits -> €270, not 10 × the per-credit rate). Server-authoritative
    so a client can't dictate the price. Falls back to the per-credit rate."""
    try:
        packs = json.loads(settings.packs_json)
    except (ValueError, TypeError):
        packs = []
    for p in packs:
        try:
            if int(p.get("n")) == n:
                amt = parse_price(p.get("price"))
                if amt and n > 0:
                    return round(amt / n)
        except (ValueError, TypeError):
            continue
    return settings.credit_price


def grant_credits(student, n, settings, *, label, sub, stripe_session_id=""):
    """Add ``n`` credits to ``student`` and issue the matching receipt + ledger
    entry, atomically. Shared by the tutor "add credits" action and the Stripe
    checkout flow so both produce identical, auditable records.

    The receipt and transaction capture a *snapshot* of the student's identity and
    billing address at issue time: these are immutable financial records that must
    stay readable verbatim even after the account is deleted (GDPR erasure).
    """
    from django.db import transaction as db_transaction
    from django.utils import timezone

    with db_transaction.atomic():
        # Lock the student row so two concurrent grants can't both read the same
        # receipt_seq and mint a duplicate receipt number.
        student = User.objects.select_for_update().get(pk=student.pk)
        student.credits += n
        student.receipt_seq += 1
        student.save()

        now = timezone.localtime()
        date_str = now.strftime("%d.%m.%Y")
        receipt_no = f"RE-{now.year}-{str(student.receipt_seq).zfill(4)}"
        student_name = student.get_full_name() or student.username

        receipt = Receipt.objects.create(
            number=receipt_no,
            student=student,
            student_slug=student.slug,
            student_name=student_name,
            billing_name=student.billing_name,
            billing_line1=student.billing_line1,
            billing_postcode=student.billing_postcode,
            billing_city=student.billing_city,
            billing_country=student.billing_country,
            date_str=date_str,
            credits=n,
            unit_price_cents=receipt_unit_price(settings, n),
            stripe_session_id=stripe_session_id,
        )

        CreditTransaction.objects.create(
            student=student,
            student_slug=student.slug,
            student_name=student_name,
            txn_type="buy",
            label=label,
            sub=sub,
            amount=n,
            receipt_no=receipt_no,
        )

    return receipt


def stripe_enabled():
    """True when self-service Stripe checkout is usable (package present + key set)."""
    return bool(stripe and dj_settings.STRIPE_SECRET_KEY)


def stripe_client():
    stripe.api_key = dj_settings.STRIPE_SECRET_KEY
    return stripe


def pack_price_cents(settings, n):
    """Total price (in cents) for a pack of ``n`` credits — server-authoritative so
    a client can never dictate what it pays. Uses the configured pack price when
    ``n`` matches a pack, else falls back to the per-credit rate × n."""
    try:
        packs = json.loads(settings.packs_json)
    except (ValueError, TypeError):
        packs = []
    for p in packs:
        try:
            if int(p.get("n")) == n:
                amt = parse_price(p.get("price"))
                if amt:
                    return round(amt * 100)
        except (ValueError, TypeError):
            continue
    return settings.credit_price * 100 * n


def credit_from_stripe_session(session):
    """Idempotently grant the credits a *paid* Checkout session represents and
    return the resulting Receipt (existing or freshly created), or None if the
    session isn't payable/identifiable. Safe to call from both the webhook and the
    post-payment redirect — the unique constraint on stripe_session_id guarantees a
    session is only ever credited once, even under a race."""
    sid = session.get("id")
    if not sid or session.get("payment_status") != "paid":
        return None
    existing = Receipt.objects.filter(stripe_session_id=sid).first()
    if existing:
        return existing
    meta = session.get("metadata") or {}
    slug = meta.get("student_slug")
    try:
        n = int(meta.get("credits", 0))
    except (ValueError, TypeError):
        n = 0
    if not slug or n <= 0:
        return None
    student = User.objects.filter(slug=slug, role="student").first()
    if not student:
        return None
    settings = get_settings()
    try:
        return grant_credits(
            student, n, settings,
            label="Credits via Stripe", sub="Online bezahlt", stripe_session_id=sid,
        )
    except IntegrityError:
        # A concurrent caller (webhook vs. redirect) won the race; reuse its receipt.
        return Receipt.objects.filter(stripe_session_id=sid).first()


def finalize_history_snapshots(user):
    """Ensure every history row tied to ``user`` carries its identity snapshot
    before the account is deleted, so SET_NULL never detaches a row to an
    anonymous, unidentifiable state. Idempotent: only fills empty snapshots."""
    name = user.get_full_name() or user.username
    for b in Booking.objects.filter(student=user, student_slug=""):
        b.student_slug, b.student_name = user.slug, name
        b.save(update_fields=["student_slug", "student_name"])
    for b in Booking.objects.filter(tutor=user, tutor_slug=""):
        b.tutor_slug, b.tutor_name = user.slug, name
        b.save(update_fields=["tutor_slug", "tutor_name"])
    CreditTransaction.objects.filter(student=user, student_slug="").update(
        student_slug=user.slug, student_name=name
    )
    # Receipts already snapshot billing at issue time; only patch a missing slug.
    Receipt.objects.filter(student=user, student_slug="").update(
        student_slug=user.slug, student_name=name
    )


def require_auth(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "auth"}, status=401)
    return None


def require_roles(*roles):
    """Endpoint guard: 401 if anonymous, 403 if the user's role isn't allowed.

    Centralizes authorization so every mutating endpoint declares exactly who
    may call it, instead of accepting any authenticated user.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({"error": "auth"}, status=401)
            if getattr(request.user, "role", None) not in roles:
                return JsonResponse({"error": "forbidden"}, status=403)
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def acting_tutor(request, slug=None):
    """The tutor a tutor/admin action is attributed to.

    A tutor always acts as themselves. An admin manages every tutor, so it must
    say which one via ``slug``; absent that, fall back to the first tutor (keeps
    single-tutor studios working without the client sending a slug).
    """
    if getattr(request.user, "role", None) == "tutor":
        return request.user
    if slug:
        return User.objects.filter(role="tutor", slug=slug).first()
    return User.objects.filter(role="tutor").first()


def parse_body(request):
    try:
        return json.loads(request.body)
    except Exception:
        return {}


def receipt_html(r_data, settings):
    """Generate receipt HTML string (mirrors JS receiptDocHtml)."""
    SUPPLIER = {
        "name": "Davit Petrosyan e.U.",
        "line2": "Englisch-Privatunterricht",
        "addr": "Lindengasse 12/4",
        "city": "1070 Wien, Österreich",
        "email": "davit@fluent.at",
    }
    VAT_DE = "Steuerfreigemäß § 6 Abs. 1 Z 11 UStG (Unterrichtsleistung eines Privatlehrers)."
    VAT_EN = "VAT-exempt educational service under Austrian law — no value-added tax is charged."

    def money(x):
        return f"{x:.2f}".replace(".", ",")

    b = r_data["billing"]
    billing_lines = ""
    if b:
        billing_lines = (
            f"{r_data['studentName']}"
            f"{'<br/>' + b['line1'] if b.get('line1') else ''}"
            f"{'<br/>' + b['postcode'] + ' ' + b['city'] if b.get('postcode') else ''}"
            f"{'<br/>' + b['country'] if b.get('country') else ''}"
        )
    else:
        billing_lines = r_data["studentName"]

    return f"""<div class="receipt-doc">
      <div class="r-top">
        <div class="r-brand"><div class="mark"></div><div><b>the green pencil</b><span>Englisch-Nachhilfe</span></div></div>
        <div class="r-meta"><div class="r-h">Beleg · Receipt</div><div>Nr. {r_data['no']}</div><div>{r_data['dateStr']}</div></div>
      </div>
      <div class="r-parties">
        <div><div class="r-lbl">Leistungserbringer · From</div>{SUPPLIER['name']}<br/>{SUPPLIER['line2']}<br/>{SUPPLIER['addr']}<br/>{SUPPLIER['city']}<br/>{SUPPLIER['email']}</div>
        <div><div class="r-lbl">Empfänger · Billed to</div>{billing_lines}</div>
      </div>
      <table class="r-table">
        <thead><tr><th>Menge</th><th>Beschreibung</th><th>Einzel</th><th>Betrag</th></tr></thead>
        <tbody><tr><td>{r_data['credits']}</td><td>Credits für Englisch-Einzelunterricht<br/><span class="r-sub">1 Credit = eine 45-Minuten-Einheit</span></td><td>€ {money(r_data['unit'])}</td><td>€ {money(r_data['net'])}</td></tr></tbody>
      </table>
      <div class="r-totals">
        <div class="r-row"><span>Nettobetrag · Net</span><span>€ {money(r_data['net'])}</span></div>
        <div class="r-row"><span>USt · VAT (0%)</span><span>€ 0,00</span></div>
        <div class="r-row r-grand"><span>Gesamt · Total</span><span>€ {money(r_data['total'])}</span></div>
      </div>
      <div class="r-vat"><b>{VAT_DE}</b><br/>{VAT_EN}</div>
      <div class="r-foot"><div>Zahlung · Payment: Externe Überweisung — bezahlt</div><div>Automatisch ausgestellter Beleg</div></div>
    </div>"""


# ---------------------------------------------------------------------------
# Main app view
# ---------------------------------------------------------------------------

def landing_view(request):
    # Public marketing landing page — the site's front door. No auth required;
    # its CTAs funnel visitors into the booking app (which gates on login).
    # Pricing mirrors the admin-configured credit packs so the public page and
    # the in-app "Credits aufladen" screen never drift apart.
    settings = get_settings()
    try:
        raw_packs = json.loads(settings.packs_json)
    except (ValueError, TypeError):
        raw_packs = []
    packs = []
    for p in raw_packs:
        try:
            n = int(p.get("n"))
        except (ValueError, TypeError):
            continue
        total = parse_price(p.get("price"))
        per_unit = round(total / n) if total and n > 0 else None
        tag = (p.get("tag") or "").strip()
        if tag.lower() == "popular":
            tag = "Beliebt"
        packs.append({
            "n": n,
            "price": p.get("price", ""),
            "per_unit": per_unit,
            "feat": bool(p.get("feat")),
            "tag": tag,
        })
    return render(request, "landing.html", {"packs": packs})


def login_view(request):
    # Standalone sign-in page. Authenticated users have no business here.
    if request.user.is_authenticated:
        return redirect("app")
    return render(request, "login.html")


def impressum_view(request):
    # Public legal pages (Impressum / Offenlegung). Required under §5 ECG, §25 MedienG.
    return render(request, "impressum.html")


def datenschutz_view(request):
    # Public privacy notice (Datenschutzerklärung). Required under GDPR Art. 13/14.
    return render(request, "datenschutz.html")


def public_booking_payload():
    """Public, PII-free data for the anonymous intro-booking calendar: every
    tutor, their availability overrides + custom times, and the slots already
    taken (date/time only, never who booked them)."""
    tutors = list(User.objects.filter(role="tutor").order_by("slug"))
    tutor_payload = [
        {
            "slug": t.slug,
            "name": t.get_full_name() or t.username,
            "firstName": (t.get_full_name() or t.username).split(" ")[0],
            "initials": t.initials,
            "color1": t.color1,
            "color2": t.color2,
        }
        for t in tutors
    ]

    availability = {}
    for ao in AvailabilityOverride.objects.all().select_related("tutor"):
        if not ao.tutor:
            continue
        k = f"{date_to_jskey(ao.date)}|{ao.time}"
        availability.setdefault(ao.tutor.slug, {})[k] = ao.is_open

    custom_times = {}
    for ct in CustomTime.objects.all().select_related("tutor"):
        if not ct.tutor:
            continue
        dk = date_to_jskey(ct.date)
        custom_times.setdefault(ct.tutor.slug, {}).setdefault(dk, []).append(ct.time)

    # Anonymized taken slots so the calendar greys them out without leaking who
    # booked. Only today onward matters for booking.
    from django.utils import timezone
    today = timezone.localdate()
    booked = {}
    for b in Booking.objects.filter(date__gte=today).select_related("tutor"):
        slug = b.tutor.slug if b.tutor_id and b.tutor else b.tutor_slug
        if slug:
            booked.setdefault(slug, []).append(f"{date_to_jskey(b.date)}|{b.time}")

    return {
        "tutors": tutor_payload,
        "availability": availability,
        "customTimes": custom_times,
        "booked": booked,
    }


def intro_view(request):
    # Public booking page: anonymous visitors pick a tutor + open slot and book a
    # free intro session. No login required; sign-in is a separate route.
    data = public_booking_payload()
    return render(request, "intro.html", {"intro_data": json.dumps(data)})


import re as _re
_TIME_RE = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Shown to guests on every booking error so they always have a way to reach a
# human: Davit's mobile and e-mail.
_INTRO_CONTACT = "Melde dich gerne bei Davit: +43 676 397 5535 oder davit@thegreenpencil.at"


def _intro_error(message, status):
    """Guest-facing booking error: always carries Davit's contact details."""
    return JsonResponse({"error": f"{message} {_INTRO_CONTACT}"}, status=status)


@require_http_methods(["POST"])
def api_intro_booking(request):
    """Public: book a free guest intro session (no account, no credits).

    CSRF-protected like every other POST (the page carries the token). A guest is
    identified only by the name + e-mail they provide here; the booking never
    touches a User row. Capped at one intro per e-mail.
    """
    data = parse_body(request)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    tutor_slug = (data.get("tutorSlug") or "").strip()
    date_key = data.get("date") or ""
    time_str = (data.get("time") or "").strip()

    if not name:
        return _intro_error("Bitte gib deinen Namen ein.", 400)
    try:
        validate_email(email)
    except ValidationError:
        return _intro_error("Bitte gib eine gültige E-Mail-Adresse ein.", 400)
    # Phone is required so the tutor can reach the guest (WhatsApp / callback).
    if len(_re.sub(r"[^0-9]", "", phone)) < 6:
        return _intro_error(
            "Bitte gib eine gültige Telefonnummer an (für WhatsApp & Rückfragen).", 400
        )
    if not _TIME_RE.match(time_str):
        return _intro_error("Ungültige Uhrzeit.", 400)

    tutor = User.objects.filter(role="tutor", slug=tutor_slug).first()
    if tutor is None:
        return _intro_error("Tutor nicht gefunden.", 400)
    try:
        booking_date = jskey_to_date(date_key)
    except (ValueError, AttributeError, TypeError):
        return _intro_error("Ungültiger Termin.", 400)

    from django.utils import timezone
    if booking_date < timezone.localdate():
        return _intro_error("Dieser Termin liegt in der Vergangenheit.", 400)

    # One free intro per e-mail *per tutor* — a guest may try a Schnupperstunde
    # with each tutor once, but not book the same tutor twice.
    if Booking.objects.filter(
        is_intro=True, tutor=tutor, guest_email__iexact=email
    ).exists():
        return _intro_error(
            "Für diese E-Mail wurde bei diesem Tutor bereits eine Schnupperstunde gebucht.",
            409,
        )
    # Slot must be free and not explicitly closed by the tutor.
    if Booking.objects.filter(tutor=tutor, date=booking_date, time=time_str).exists():
        return _intro_error("Dieser Termin ist bereits vergeben.", 409)
    if AvailabilityOverride.objects.filter(
        tutor=tutor, date=booking_date, time=time_str, is_open=False
    ).exists():
        return _intro_error("Dieser Termin ist nicht verfügbar.", 409)

    booking = Booking.objects.create(
        tutor=tutor, student=None,
        date=booking_date, time=time_str,
        title="Schnupperstunde (Intro)",
        is_intro=True, guest_name=name, guest_email=email, guest_phone=phone,
        # Snapshot so the tutor calendar can show the guest without a User row.
        student_name=name, student_slug="intro",
    )
    # Fire-and-forget: a mail hiccup must never fail the booking itself.
    from . import emails
    emails.queue_email(emails.send_intro_confirmation, booking.pk)
    emails.queue_email(emails.send_intro_tutor_notification, booking.pk)
    return JsonResponse({
        "ok": True,
        "tutorName": tutor.get_full_name() or tutor.username,
        "date": booking_date.isoformat(),
        "time": time_str,
    })


def app_view(request):
    if not request.user.is_authenticated:
        return redirect("login")

    user = request.user
    settings = get_settings()

    students = list(User.objects.filter(role="student").order_by("slug"))
    tutors = list(User.objects.filter(role="tutor").order_by("slug"))

    # Scope the payload by role. A logged-in student must not receive other
    # students' PII (credits, billing, transaction logs, notes), but the
    # calendar still needs the tutor's other bookings so overlapping slots are
    # blocked — those are sent anonymized (no identity, no notes).
    is_student = user.role == "student"
    visible_students = [user] if is_student else students

    if is_student:
        own_bookings = list(
            Booking.objects.filter(student=user).select_related("student", "tutor")
        )
        other_bookings = list(
            Booking.objects.filter(tutor__role="tutor")
            .exclude(student=user)
            .select_related("tutor")
        )
        bookings_payload = [serialize_booking(b) for b in own_bookings] + [
            blocker_booking(b) for b in other_bookings
        ]
    else:
        bookings_payload = [
            serialize_booking(b)
            for b in Booking.objects.all().select_related("student", "tutor")
        ]

    # Transactions: per visible student slug
    transactions = {}
    for s in visible_students:
        txns = list(s.transactions.all())
        transactions[s.slug] = [serialize_transaction(t) for t in txns]

    # Receipts: only the viewer's own for students; all for tutor/admin
    if is_student:
        all_receipts = list(Receipt.objects.filter(student=user).select_related("student"))
    else:
        all_receipts = list(Receipt.objects.all().select_related("student"))
    receipts_data = [serialize_receipt(r) for r in all_receipts]

    # Availability overrides, keyed per tutor so two tutors' calendars never
    # collide: {tutor_slug: {date_key|time: is_open}}
    availability = {}
    for ao in AvailabilityOverride.objects.all().select_related("tutor"):
        # date_key format: "YYYY-M-D" (matches JS keyOf, 0-indexed month)
        k = f"{date_to_jskey(ao.date)}|{ao.time}"
        availability.setdefault(ao.tutor.slug, {})[k] = ao.is_open

    # Custom times, also per tutor: {tutor_slug: {date_key: [times]}}
    custom_times = {}
    for ct in CustomTime.objects.all().select_related("tutor"):
        dk = date_to_jskey(ct.date)
        custom_times.setdefault(ct.tutor.slug, {}).setdefault(dk, []).append(ct.time)

    # Student notes are tutor-private — never expose them to a student client.
    student_notes = {}
    if not is_student:
        for s in visible_students:
            notes = list(StudentNote.objects.filter(student=s).order_by("-created_at"))
            student_notes[s.slug] = [
                {"date": n.created_at.strftime("%d.%m.%Y"), "text": n.text}
                for n in notes
            ]

    # Active lessons: {slug: [lesson_ids]}
    active_lessons = {}
    for s in visible_students:
        ids = list(ActiveLesson.objects.filter(student=s).values_list("lesson_id", flat=True))
        active_lessons[s.slug] = ids

    # Lesson PDFs: {lesson_id: [{id,name,url}]}. A student only receives files for
    # lessons they've unlocked; tutor/admin get the full set to manage.
    lesson_files = {}
    if is_student:
        my_ids = set(active_lessons.get(user.slug, []))
        lf_qs = LessonFile.objects.filter(lesson_id__in=my_ids) if my_ids else LessonFile.objects.none()
    else:
        lf_qs = LessonFile.objects.all()
    for lf in lf_qs:
        lesson_files.setdefault(lf.lesson_id, []).append(serialize_lesson_file(lf))

    # Settings
    packs = json.loads(settings.packs_json)
    # receiptSeq: compute from max receipt number
    try:
        import re
        max_seq = 1000
        for r in all_receipts:
            m = re.search(r"-(\d+)$", r.number)
            if m:
                max_seq = max(max_seq, int(m.group(1)) + 1)
    except Exception:
        max_seq = 1042

    django_data = {
        "isAuthenticated": True,
        "role": user.role,
        "currentUserId": user.slug,
        "currentUser": serialize_user(user),
        "students": [serialize_user(s) for s in visible_students],
        "tutors": [serialize_user(t) for t in tutors],
        "bookings": bookings_payload,
        "transactions": transactions,
        "receipts": receipts_data,
        "availability": availability,
        "customTimes": custom_times,
        "studentNotes": student_notes,
        "activeLessons": active_lessons,
        "lessonFiles": lesson_files,
        "settings": {
            "creditPrice": settings.credit_price,
            "packs": packs,
            "receiptSeq": max_seq,
        },
        "stripe": {
            "enabled": stripe_enabled(),
            "publishableKey": dj_settings.STRIPE_PUBLISHABLE_KEY,
        },
    }

    return render(request, "app.html", {"django_data": json.dumps(django_data), "role": user.role})


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
def api_login(request):
    data = parse_body(request)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email:
        return JsonResponse({"error": "Gib deine E-Mail ein"}, status=400)
    # Single generic message for both unknown-email and wrong-password so the
    # endpoint can't be used to enumerate which emails have accounts.
    INVALID = "E-Mail oder Passwort ungültig."
    user_obj = User.objects.filter(email__iexact=email).first()
    if user_obj is not None:
        user_obj = authenticate(request, username=user_obj.username, password=password)
    if user_obj is None:
        return JsonResponse({"error": INVALID}, status=400)
    login(request, user_obj)
    return JsonResponse({"ok": True, "role": user_obj.role, "slug": user_obj.slug})


@require_http_methods(["POST"])
def api_logout(request):
    logout(request)
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Bookings API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("student", "tutor", "admin")
def api_bookings(request):
    data = parse_body(request)
    try:
        student = User.objects.get(slug=data["studentSlug"], role="student")
        tutor = User.objects.get(slug=data["tutorSlug"], role="tutor")
        # A student may only create bookings for themselves; tutor/admin for anyone.
        if request.user.role == "student" and student != request.user:
            return JsonResponse({"error": "forbidden"}, status=403)
        booking_date = date.fromisoformat(data["date"])
        time_str = data["time"]
        title = data.get("title", "English session")
        b = Booking.objects.create(
            student=student,
            tutor=tutor,
            date=booking_date,
            time=time_str,
            title=title,
        )
        return JsonResponse({"pk": b.pk})
    except (KeyError, User.DoesNotExist, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=400)


@require_http_methods(["PUT", "DELETE"])
@require_roles("student", "tutor", "admin")
def api_booking_detail(request, pk):
    try:
        b = Booking.objects.get(pk=pk)
    except Booking.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    # Object-level check: a student may only touch their own bookings (prevents
    # IDOR — editing/cancelling another student's session by guessing its pk).
    is_student = request.user.role == "student"
    if is_student and b.student != request.user:
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.method == "DELETE":
        b.delete()
        return JsonResponse({"ok": True})

    # PUT
    data = parse_body(request)
    if "title" in data:
        b.title = data["title"]
    if "date" in data:
        b.date = date.fromisoformat(data["date"])
    if "time" in data:
        b.time = data["time"]
    if "notes" in data:
        b.notes = data["notes"]
    # tutorNotes and callLink are tutor-owned fields — students can't set them.
    if not is_student:
        if "tutorNotes" in data:
            b.tutor_notes = data["tutorNotes"]
        if "callLink" in data:
            b.call_link = data["callLink"]
    b.save()
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Credits API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_credits(request, slug):
    data = parse_body(request)
    try:
        student = User.objects.get(slug=slug, role="student")
        n = int(data.get("n", 1))
        if n <= 0:
            raise ValueError("n must be positive")
    except (User.DoesNotExist, ValueError, TypeError) as e:
        return JsonResponse({"error": str(e)}, status=400)

    settings = get_settings()
    receipt = grant_credits(
        student, n, settings,
        label="Credits added by tutor",
        sub="Today · paid externally",
    )
    receipt_no = receipt.number

    r_data = serialize_receipt(receipt)
    html = receipt_html(r_data, settings)
    return JsonResponse({"receiptNo": receipt_no, "receiptHtml": html})


# ---------------------------------------------------------------------------
# Billing API
# ---------------------------------------------------------------------------

@require_http_methods(["PUT"])
def api_billing(request):
    err = require_auth(request)
    if err:
        return err
    data = parse_body(request)
    user = request.user
    if "name" in data:
        parts = (data["name"] or "").strip().split(" ", 1)
        user.first_name = parts[0]
        user.last_name = parts[1] if len(parts) > 1 else ""
        user.billing_name = data["name"]
        user.initials = compute_initials(data["name"])
    if "line1" in data:
        user.billing_line1 = data["line1"]
    if "postcode" in data:
        user.billing_postcode = data["postcode"]
    if "city" in data:
        user.billing_city = data["city"]
    if "country" in data:
        user.billing_country = data["country"] or "Österreich"
    user.save()
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Stripe checkout API (self-service credit top-ups)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("student")
def api_checkout(request):
    """Create a Stripe Checkout session for the current student to buy ``n``
    credits, and return its hosted-payment URL. Price is computed server-side."""
    if not stripe_enabled():
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    data = parse_body(request)
    try:
        n = int(data.get("n", 1))
        if n <= 0:
            raise ValueError("n must be positive")
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid amount"}, status=400)

    settings = get_settings()
    amount = pack_price_cents(settings, n)
    origin = request.build_absolute_uri("/").rstrip("/")
    client = stripe_client()
    try:
        session = client.checkout.Session.create(
            mode="payment",
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": "eur",
                    "unit_amount": amount,
                    "product_data": {
                        "name": f"{n} Credits — the green pencil",
                        "description": "1 Credit = eine 45-Minuten-Einheit Englisch-Einzelunterricht",
                    },
                },
            }],
            # Server-trusted facts the webhook/redirect use to credit the right
            # student. The price is set above, not taken from the client.
            metadata={"student_slug": request.user.slug, "credits": str(n)},
            client_reference_id=request.user.slug,
            customer_email=request.user.email or None,
            success_url=f"{origin}/app/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/app/?checkout=cancel",
        )
    except Exception:
        # Don't leak Stripe internals to the client; the UI falls back to e-mail.
        return JsonResponse({"error": "stripe_error"}, status=502)
    return JsonResponse({"url": session.url, "id": session.id})


@require_http_methods(["POST"])
@require_roles("student")
def api_checkout_confirm(request):
    """Called when the student returns from Stripe. Verifies the session was paid
    and credits them if a webhook hasn't already. Idempotent."""
    if not stripe_enabled():
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    data = parse_body(request)
    sid = (data.get("sessionId") or "").strip()
    if not sid:
        return JsonResponse({"error": "missing session"}, status=400)
    client = stripe_client()
    try:
        session = client.checkout.Session.retrieve(sid)
    except Exception:
        return JsonResponse({"error": "not found"}, status=404)
    # A student may only confirm a session that was created for them.
    meta = session.get("metadata") or {}
    if meta.get("student_slug") != request.user.slug:
        return JsonResponse({"error": "forbidden"}, status=403)
    receipt = credit_from_stripe_session(session)
    if not receipt:
        return JsonResponse({"paid": False})
    request.user.refresh_from_db()
    return JsonResponse({
        "paid": True,
        "credits": request.user.credits,
        "receipt": serialize_receipt(receipt),
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_stripe_webhook(request):
    """Stripe -> us. Verifies the signature (when a webhook secret is configured)
    and credits the student on checkout.session.completed. The source of truth for
    crediting; the post-payment redirect is only a faster-feeling fallback."""
    if not stripe_enabled():
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    payload = request.body
    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    secret = dj_settings.STRIPE_WEBHOOK_SECRET
    client = stripe_client()
    try:
        if secret:
            # Raises on a bad payload or a signature that doesn't match the secret
            # (i.e. a forged event). Exception type varies across SDK versions, so
            # catch broadly here — this block only constructs the event.
            event = client.Webhook.construct_event(payload, sig, secret)
        else:
            # No secret configured (e.g. local dev): accept unverified JSON.
            event = json.loads(payload)
    except Exception:
        return JsonResponse({"error": "invalid"}, status=400)
    event_type = event.get("type") if isinstance(event, dict) else event["type"]
    if event_type == "checkout.session.completed":
        credit_from_stripe_session(event["data"]["object"])
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Availability API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_availability(request):
    data = parse_body(request)
    try:
        # date_key is "YYYY-M-D" (JS keyOf format, 0-indexed month)
        d = jskey_to_date(data["date"])
        time_str = data["time"]
        is_open = bool(data.get("isOpen", True))
        tutor = acting_tutor(request, data.get("tutorSlug"))
        if tutor is None:
            return JsonResponse({"error": "unknown tutor"}, status=400)
        AvailabilityOverride.objects.update_or_create(
            tutor=tutor,
            date=d,
            time=time_str,
            defaults={"is_open": is_open},
        )
        return JsonResponse({"ok": True})
    except (KeyError, ValueError, IndexError) as e:
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Custom Times API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_custom_times(request):
    data = parse_body(request)
    try:
        d = jskey_to_date(data["date"])
        time_str = data["time"]
        tutor = acting_tutor(request, data.get("tutorSlug"))
        if tutor is None:
            return JsonResponse({"error": "unknown tutor"}, status=400)
        CustomTime.objects.get_or_create(tutor=tutor, date=d, time=time_str)
        return JsonResponse({"ok": True})
    except (KeyError, ValueError, IndexError) as e:
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Notes API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_notes(request, slug):
    data = parse_body(request)
    try:
        student = User.objects.get(slug=slug, role="student")
        text = (data.get("text") or "").strip()
        if not text:
            return JsonResponse({"error": "text required"}, status=400)
        tutor = acting_tutor(request)
        note = StudentNote.objects.create(tutor=tutor, student=student, text=text)
        return JsonResponse({"ok": True, "date": note.created_at.strftime("%d.%m.%Y")})
    except User.DoesNotExist as e:
        return JsonResponse({"error": str(e)}, status=404)


# ---------------------------------------------------------------------------
# Lessons API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_lessons(request, slug):
    data = parse_body(request)
    try:
        student = User.objects.get(slug=slug, role="student")
        lesson_id = data.get("lessonId", "")
        on = data.get("on")
        if on is None:
            # Toggle
            exists = ActiveLesson.objects.filter(student=student, lesson_id=lesson_id).exists()
            on = not exists
        if on:
            ActiveLesson.objects.get_or_create(student=student, lesson_id=lesson_id)
        else:
            ActiveLesson.objects.filter(student=student, lesson_id=lesson_id).delete()
        return JsonResponse({"ok": True})
    except User.DoesNotExist as e:
        return JsonResponse({"error": str(e)}, status=404)


# ---------------------------------------------------------------------------
# Lesson files API (PDF materials, shared per lesson)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_lesson_files(request, lesson_id):
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "Keine Datei hochgeladen."}, status=400)
    name = f.name or "file"
    if file_ext(name) not in ALLOWED_LESSON_EXTS:
        return JsonResponse(
            {"error": "Dateityp nicht unterstützt. Erlaubt: PDF, Office-Dokumente, Bilder, Audio, ZIP."},
            status=400,
        )
    if f.size > MAX_LESSON_FILE_BYTES:
        return JsonResponse({"error": "Datei zu groß (max. 25 MB)."}, status=400)
    lf = LessonFile.objects.create(
        lesson_id=lesson_id, file=f, original_name=name[:255], uploaded_by=request.user,
    )
    return JsonResponse(serialize_lesson_file(lf))


@require_http_methods(["DELETE"])
@require_roles("tutor", "admin")
def api_lesson_file_detail(request, file_id):
    try:
        lf = LessonFile.objects.get(pk=file_id)
    except LessonFile.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)
    lf.file.delete(save=False)  # remove the blob from storage too
    lf.delete()
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def api_lesson_file_download(request, file_id):
    # Access-controlled file serving (no public MEDIA URL): students may only
    # download materials for lessons they have unlocked; tutor/admin always.
    if not request.user.is_authenticated:
        return JsonResponse({"error": "auth"}, status=401)
    try:
        lf = LessonFile.objects.get(pk=file_id)
    except LessonFile.DoesNotExist:
        raise Http404
    if request.user.role == "student" and not ActiveLesson.objects.filter(
        student=request.user, lesson_id=lf.lesson_id
    ).exists():
        return JsonResponse({"error": "forbidden"}, status=403)
    import mimetypes
    ctype = mimetypes.guess_type(lf.original_name)[0] or "application/octet-stream"
    try:
        resp = FileResponse(lf.file.open("rb"), content_type=ctype)
    except FileNotFoundError:
        raise Http404
    # Sanitize the user-supplied filename before putting it in a header. Served
    # as an attachment (+ global nosniff) so nothing renders inline.
    safe = lf.original_name.replace('"', "").replace("\r", "").replace("\n", "") or "lesson"
    resp["Content-Disposition"] = f'attachment; filename="{safe}"'
    return resp


# ---------------------------------------------------------------------------
# Users API (admin)
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
@require_roles("admin")
def api_users(request):
    if request.method == "GET":
        users = list(User.objects.all())
        return JsonResponse({"users": [serialize_user(u) for u in users]})

    # POST: create a student (default) or a tutor
    import time as time_module
    data = parse_body(request)
    role = data.get("role", "student")
    if role not in ("student", "tutor"):
        return JsonResponse({"error": "invalid role"}, status=400)

    # Unique, collision-safe slug. The timestamp tail is near-unique, but two
    # rapid creates could clash — loop until the slug is actually free.
    prefix = "tut" if role == "tutor" else "stu"
    base = prefix + str(int(time_module.time() * 1000))[-8:]
    slug = base
    n = 2
    while User.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1

    name = (data.get("name") or "").strip()
    first_name, _, last_name = name.partition(" ")
    # Each account gets a unique, unguessable temporary password — never a shared
    # default. It is returned once (below) so the admin can hand it over; it is not
    # stored in clear text and can't be read back afterwards.
    temp_password = secrets.token_urlsafe(9)

    if role == "tutor":
        new_user = User.objects.create_user(
            username=slug, email=f"{slug}@fluent.at", password=temp_password,
            role="tutor", slug=slug,
            initials=compute_initials(name) if name else "NT",
            color1="#309050", color2="#277a42",
        )
        new_user.first_name = first_name or "New"
        new_user.last_name = last_name or "Tutor"
    else:
        new_user = User.objects.create_user(
            username=slug, email=f"{slug}@fluent.at", password=temp_password,
            role="student", slug=slug,
            initials=compute_initials(name) if name else "NS",
            color1="#52a86a", color2="#2f8a4d",
        )
        new_user.first_name = first_name or "New"
        new_user.last_name = last_name or "Student"
    new_user.save()
    payload = serialize_user(new_user)
    payload["tempPassword"] = temp_password  # shown to the admin once, then discarded
    return JsonResponse(payload)


@require_http_methods(["PUT", "DELETE"])
@require_roles("admin")
def api_user_detail(request, slug):
    try:
        u = User.objects.get(slug=slug)
    except User.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    if request.method == "DELETE":
        # Don't let an admin delete their own account out from under themselves.
        if u == request.user:
            return JsonResponse({"error": "Du kannst dein eigenes Konto nicht löschen."}, status=400)
        # GDPR "anonymise & keep": the account (and its PII — email, photo, notes)
        # is erased, but the financial/lesson history is statutorily retained. The
        # history models use on_delete=SET_NULL, so deleting the user detaches the
        # FK while leaving the rows — and their frozen identity/billing snapshots —
        # untouched. We finalise any missing snapshot first so nothing detaches to
        # an unidentifiable record.
        finalize_history_snapshots(u)
        u.delete()
        return JsonResponse({"ok": True})

    # PUT
    data = parse_body(request)
    if "name" in data:
        name = data["name"] or ""
        parts = name.strip().split(" ", 1)
        u.first_name = parts[0]
        u.last_name = parts[1] if len(parts) > 1 else ""
        u.billing_name = name
        u.initials = compute_initials(name)
    if "email" in data:
        new_email = (data["email"] or "").strip().lower()
        # Email is the login identifier — keep it unique to avoid ambiguous logins.
        if new_email and User.objects.filter(email__iexact=new_email).exclude(pk=u.pk).exists():
            return JsonResponse({"error": "Diese E-Mail wird bereits verwendet."}, status=400)
        u.email = new_email
    if "password" in data and data["password"]:
        u.set_password(data["password"])
    if "credits" in data:
        try:
            u.credits = int(data["credits"])
        except (ValueError, TypeError):
            pass
    if "billing" in data:
        b = data["billing"]
        if isinstance(b, dict):
            if "line1" in b:
                u.billing_line1 = b["line1"]
            if "postcode" in b:
                u.billing_postcode = b["postcode"]
            if "city" in b:
                u.billing_city = b["city"]
            if "country" in b:
                u.billing_country = b["country"] or "Österreich"
    u.save()
    return JsonResponse(serialize_user(u))


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "PUT"])
@require_roles("admin")
def api_settings(request):
    settings = get_settings()

    if request.method == "GET":
        return JsonResponse({
            "creditPrice": settings.credit_price,
            "packs": json.loads(settings.packs_json),
        })

    # PUT
    data = parse_body(request)
    if "creditPrice" in data:
        try:
            v = int(data["creditPrice"])
            if v > 0:
                settings.credit_price = v
        except (ValueError, TypeError):
            pass
    if "packs" in data:
        settings.packs_json = json.dumps(data["packs"])
    settings.save()
    return JsonResponse({"ok": True})
