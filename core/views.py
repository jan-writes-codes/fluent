import json
from datetime import date
from functools import wraps
from django.shortcuts import render, redirect
from django.http import JsonResponse, FileResponse, Http404
from django.contrib.auth import authenticate, login, logout
from django.views.decorators.http import require_http_methods
from .models import (
    User, Booking, CreditTransaction, Receipt, AvailabilityOverride,
    CustomTime, StudentNote, ActiveLesson, SiteSettings, LessonFile
)

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


def serialize_booking(b):
    return {
        "pk": b.pk,
        "studentId": b.student.slug,
        "tutorId": b.tutor.slug,
        "date": b.date.isoformat(),
        "time": b.time,
        "title": b.title,
        "notes": b.notes,
        "tutorNotes": b.tutor_notes,
        "callLink": b.call_link,
    }


def blocker_booking(b):
    """Anonymized booking sent to a student so the calendar blocks the slot
    without revealing who booked it or any session details."""
    return {
        "pk": None,
        "studentId": "__blocked__",
        "tutorId": b.tutor.slug,
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
    return {
        "no": r.number,
        "dateStr": r.date_str,
        "studentId": r.student.slug,
        "studentName": r.student.get_full_name() or r.student.username,
        "billing": {
            "name": r.student.billing_name,
            "line1": r.student.billing_line1,
            "postcode": r.student.billing_postcode,
            "city": r.student.billing_city,
            "country": r.student.billing_country,
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


def acting_tutor(request):
    """The tutor a tutor/admin action is attributed to (single-tutor studio)."""
    if getattr(request.user, "role", None) == "tutor":
        return request.user
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
        <div class="r-brand"><div class="mark"></div><div><b>fluent.</b><span>English tutoring</span></div></div>
        <div class="r-meta"><div class="r-h">Beleg · Receipt</div><div>Nr. {r_data['no']}</div><div>{r_data['dateStr']}</div></div>
      </div>
      <div class="r-parties">
        <div><div class="r-lbl">Leistungserbringer · From</div>{SUPPLIER['name']}<br/>{SUPPLIER['line2']}<br/>{SUPPLIER['addr']}<br/>{SUPPLIER['city']}<br/>{SUPPLIER['email']}</div>
        <div><div class="r-lbl">Empfänger · Billed to</div>{billing_lines}</div>
      </div>
      <table class="r-table">
        <thead><tr><th>Menge</th><th>Beschreibung</th><th>Einzel</th><th>Betrag</th></tr></thead>
        <tbody><tr><td>{r_data['credits']}</td><td>Guthaben für Englisch-Einzelunterricht<br/><span class="r-sub">1 Guthaben = eine 50-Minuten-Einheit</span></td><td>€ {money(r_data['unit'])}</td><td>€ {money(r_data['net'])}</td></tr></tbody>
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
    return render(request, "landing.html")


def login_view(request):
    # Standalone sign-in page. Authenticated users have no business here.
    if request.user.is_authenticated:
        return redirect("app")
    return render(request, "login.html")


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

    # Availability overrides: {date_key|time: is_open}
    availability = {}
    for ao in AvailabilityOverride.objects.all().select_related("tutor"):
        # date_key format: "YYYY-M-D" (matches JS keyOf, 0-indexed month)
        k = f"{date_to_jskey(ao.date)}|{ao.time}"
        availability[k] = ao.is_open

    # Custom times: {date_key: [times]}
    custom_times = {}
    for ct in CustomTime.objects.all().select_related("tutor"):
        dk = date_to_jskey(ct.date)
        if dk not in custom_times:
            custom_times[dk] = []
        custom_times[dk].append(ct.time)

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
        return JsonResponse({"error": "Enter your email"}, status=400)
    # Single generic message for both unknown-email and wrong-password so the
    # endpoint can't be used to enumerate which emails have accounts.
    INVALID = "Invalid email or password."
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
    student.credits += n
    student.receipt_seq += 1
    student.save()

    from django.utils import timezone
    now = timezone.localtime()
    date_str = now.strftime("%d.%m.%Y")
    receipt_no = f"RE-{now.year}-{str(student.receipt_seq).zfill(4)}"

    receipt = Receipt.objects.create(
        number=receipt_no,
        student=student,
        date_str=date_str,
        credits=n,
        unit_price_cents=receipt_unit_price(settings, n),
    )

    CreditTransaction.objects.create(
        student=student,
        txn_type="buy",
        label="Credits added by tutor",
        sub=f"Today · paid externally",
        amount=n,
        receipt_no=receipt_no,
    )

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
        tutor = acting_tutor(request)
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
        tutor = acting_tutor(request)
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
        return JsonResponse({"error": "No file uploaded."}, status=400)
    name = f.name or "file"
    if file_ext(name) not in ALLOWED_LESSON_EXTS:
        return JsonResponse(
            {"error": "Unsupported file type. Allowed: PDF, Office docs, images, audio, zip."},
            status=400,
        )
    if f.size > MAX_LESSON_FILE_BYTES:
        return JsonResponse({"error": "File too large (max 25 MB)."}, status=400)
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

    # POST: create student
    import time as time_module
    data = parse_body(request)
    slug = "stu" + str(int(time_module.time() * 1000))[-8:]
    new_user = User.objects.create_user(
        username=slug,
        email=f"{slug}@fluent.at",
        password="password",
        role="student",
        slug=slug,
        initials="NS",
        color1="#9aa0a6",
        color2="#6b7177",
    )
    new_user.first_name = "New"
    new_user.last_name = "Student"
    new_user.save()
    return JsonResponse(serialize_user(new_user))


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
            return JsonResponse({"error": "You can't delete your own account."}, status=400)
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
            return JsonResponse({"error": "That email is already in use."}, status=400)
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
