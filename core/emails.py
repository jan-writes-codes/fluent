"""Transactional e-mail for the booking flow.

Sent through Django's mail framework, so the backend is swappable: Resend (via
django-anymail) in production, console in dev, locmem in tests. Nothing here
requires e-mail to be configured — if it isn't, mail is printed to the console
and the booking still succeeds.

Each public entry point takes a booking *id* (not the object) so it is safe to
run on a background thread or, later, a durable queue (see ``queue_email``).
"""
import logging
import threading
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Booking

logger = logging.getLogger(__name__)

VIENNA = ZoneInfo("Europe/Vienna")
UTC = ZoneInfo("UTC")
_DOW = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MON = ["", "Jänner", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
        "September", "Oktober", "November", "Dezember"]


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
# An intro/Schnupperstunde runs 15 minutes; a paid credit lesson is one
# 45-minute unit (1 Einheit = 45 Minuten Unterricht).
INTRO_MINUTES = 15
LESSON_MINUTES = 45


def _end_time(hhmm, minutes=INTRO_MINUTES):
    h, m = (int(x) for x in hhmm.split(":"))
    total = h * 60 + m + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def _date_long(d):
    return f"{_DOW[d.weekday()]}, {d.day}. {_MON[d.month]} {d.year}"


def booking_when(booking):
    """Human German date/time line, e.g. 'Montag, 1. Juli 2026 · 14:00–14:15'."""
    return f"{_date_long(booking.date)} · {booking.time}–{_end_time(booking.time)}"


def lesson_when(booking):
    """Date/time line for a 45-minute credit lesson, e.g. '… · 09:00–09:45'."""
    return f"{_date_long(booking.date)} · {booking.time}–{_end_time(booking.time, LESSON_MINUTES)}"


def build_ics(booking):
    """A minimal, valid VCALENDAR for the lesson so the guest can add it to their
    calendar in one tap. Times are emitted in UTC to avoid shipping a VTIMEZONE."""
    start_local = datetime.combine(
        booking.date, dt_time.fromisoformat(booking.time)
    ).replace(tzinfo=VIENNA)
    start = start_local.astimezone(UTC)
    end = start + timedelta(minutes=15)
    stamp = timezone.now().astimezone(UTC)
    fmt = "%Y%m%dT%H%M%SZ"
    tutor = booking.tutor_name or (booking.tutor.get_full_name() if booking.tutor_id and booking.tutor else "The Green Pencil")
    organizer = settings.EMAIL_REPLY_TO or "hallo@thegreenpencil.at"

    def esc(s):
        return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//The Green Pencil//Booking//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:intro-{booking.pk}@thegreenpencil.at",
        f"DTSTAMP:{stamp.strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}",
        f"DTEND:{end.strftime(fmt)}",
        f"SUMMARY:{esc('Englisch Schnupperstunde · ' + tutor)}",
        f"DESCRIPTION:{esc('Deine kostenlose Englisch-Schnupperstunde mit ' + tutor + ' bei The Green Pencil.')}",
        f"ORGANIZER;CN={esc('The Green Pencil')}:mailto:{organizer}",
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    # iCalendar lines are CRLF-terminated.
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------- #
# Senders (take an id so they're safe to run off-thread)
# --------------------------------------------------------------------------- #
def _ctx(booking):
    tutor = booking.tutor_name or (
        booking.tutor.get_full_name() if booking.tutor_id and booking.tutor else "deinem Tutor"
    )
    return {
        "guest_name": booking.guest_name,
        "guest_first": (booking.guest_name or "").split(" ")[0] or "du",
        "guest_email": booking.guest_email,
        "guest_phone": booking.guest_phone,
        "tutor_name": tutor,
        "when": booking_when(booking),
        "date_long": _date_long(booking.date),
        "time_range": f"{booking.time}–{_end_time(booking.time)}",
        "site_url": settings.SITE_URL,
    }


def _message(subject, to, text_body, html_body, reply_to=None):
    reply = reply_to if reply_to else (
        [settings.EMAIL_REPLY_TO] if settings.EMAIL_REPLY_TO else None
    )
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to if isinstance(to, (list, tuple)) else [to],
        reply_to=reply,
    )
    msg.attach_alternative(html_body, "text/html")
    return msg


def send_intro_confirmation(booking_id):
    """Confirmation to the guest, with the lesson as a calendar attachment."""
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking or not booking.guest_email:
        return
    ctx = _ctx(booking)
    subject = f"Deine Schnupperstunde ist bestätigt · {ctx['date_long']}"
    msg = _message(
        subject, booking.guest_email,
        render_to_string("email/intro_confirmation.txt", ctx),
        render_to_string("email/intro_confirmation.html", ctx),
    )
    msg.attach("schnupperstunde.ics", build_ics(booking), "text/calendar; method=REQUEST")
    msg.send()


def send_intro_tutor_notification(booking_id):
    """Alert the studio inbox that a new intro was booked. No-op if unconfigured."""
    to = settings.TUTOR_NOTIFY_EMAIL
    if not to:
        return
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking:
        return
    ctx = _ctx(booking)
    subject = f"Neue Schnupperstunde: {ctx['guest_name']} · {ctx['date_long']}"
    msg = _message(
        subject, to,
        render_to_string("email/intro_tutor.txt", ctx),
        render_to_string("email/intro_tutor.html", ctx),
    )
    msg.send()


def _lesson_ctx(booking):
    student = booking.student if booking.student_id else None
    student_name = (
        (student.get_full_name() or student.username) if student else booking.student_name
    ) or "Ein Schüler"
    student_email = (student.email if student else "") or ""
    tutor = booking.tutor_name or (
        booking.tutor.get_full_name() if booking.tutor_id and booking.tutor else "Tutor"
    )
    return {
        "student_name": student_name,
        "student_first": (student_name or "").split(" ")[0] or "dein Schüler",
        "student_email": student_email,
        "tutor_name": tutor,
        "title": booking.title,
        "when": lesson_when(booking),
        "date_long": _date_long(booking.date),
        "time_range": f"{booking.time}–{_end_time(booking.time, LESSON_MINUTES)}",
        "site_url": settings.SITE_URL,
    }


def send_lesson_tutor_notification(booking_id):
    """Notify the tutor that a student booked (and spent a credit on) a lesson.

    Goes to the tutor's own e-mail address — not the studio-wide
    ``TUTOR_NOTIFY_EMAIL`` used for intros — so the right tutor hears about
    their own bookings. No-op for intros or if the tutor has no address."""
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking or booking.is_intro:
        return
    to = (booking.tutor.email if booking.tutor_id and booking.tutor else "") or ""
    if not to:
        return
    ctx = _lesson_ctx(booking)
    subject = f"Neue Buchung: {ctx['student_name']} · {ctx['date_long']} {ctx['time_range']}"
    # Reply goes to the student so the tutor can answer directly.
    reply_to = [ctx["student_email"]] if ctx["student_email"] else None
    msg = _message(
        subject, to,
        render_to_string("email/lesson_tutor.txt", ctx),
        render_to_string("email/lesson_tutor.html", ctx),
        reply_to=reply_to,
    )
    msg.send()


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def _safe(func, *args):
    try:
        func(*args)
    except Exception:  # never let an e-mail failure break the booking
        logger.exception("transactional e-mail failed: %s", getattr(func, "__name__", func))


def queue_email(func, *args):
    """Run a sender without blocking the request. A daemon thread is enough for a
    single-studio app; swap this one function for a durable queue (Django Q2 /
    Celery) when volume warrants — the call sites don't change."""
    if getattr(settings, "EMAIL_ASYNC", False):
        threading.Thread(target=_safe, args=(func, *args), daemon=True).start()
    else:
        _safe(func, *args)
