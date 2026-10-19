"""The tutor's private iCal subscription feed.

Served at /calendar/<token>.ics — the unguessable token on the tutor's User
row is the whole capability (same pattern as settle/cancel tokens), so Apple/
Google/Outlook can poll the URL without any login. One VEVENT per booking;
times are emitted in UTC so no VTIMEZONE needs shipping (mirrors the
single-event .ics that ``emails.build_ics`` attaches to confirmations).
"""
from datetime import timedelta, timezone as dt_timezone

from django.utils import timezone

_ICS_FMT = "%Y%m%dT%H%M%SZ"


def ics_escape(s):
    # A bare CR would end the content line, so normalise line breaks first.
    s = str(s or "").replace("\r\n", "\n").replace("\r", "\n")
    return (s.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def ics_fold(line):
    """Fold a content line to at most 75 octets (RFC 5545 §3.1): continuation
    lines start with a space, and a UTF-8 character is never split."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            out.append(cur)
            cur, size = " ", 1
        cur += ch
        size += n
    out.append(cur)
    return "\r\n".join(out)


def ics_join(lines):
    """Serialise content lines: folded and CRLF-terminated."""
    return "\r\n".join(ics_fold(l) for l in lines) + "\r\n"


def _event_lines(booking, stamp):
    start = booking.start.astimezone(dt_timezone.utc)
    end = start + timedelta(minutes=booking.minutes)

    with_name = booking.guest_name or booking.group_name or booking.student_name or ""
    summary = f"{booking.title} · {with_name}" if with_name else booking.title
    if booking.is_requested:
        summary = f"Anfrage: {summary}"  # still awaiting the tutor's confirmation

    # Everything the tutor needs at a glance in the calendar entry: who the
    # guest is (intros carry contact details), their notes, and the call link.
    desc_parts = []
    if booking.is_intro:
        contact = " · ".join(p for p in (booking.guest_email, booking.guest_phone) if p)
        if contact:
            desc_parts.append(f"Gast: {contact}")
    if booking.notes:
        desc_parts.append(booking.notes)
    if booking.join_link:
        desc_parts.append(f"Call-Link: {booking.join_link}")

    lines = [
        "BEGIN:VEVENT",
        f"UID:booking-{booking.pk}@thegreenpencil.at",
        f"DTSTAMP:{stamp.strftime(_ICS_FMT)}",
        f"DTSTART:{start.strftime(_ICS_FMT)}",
        f"DTEND:{end.strftime(_ICS_FMT)}",
        f"SUMMARY:{ics_escape(summary)}",
    ]
    if desc_parts:
        lines.append(f"DESCRIPTION:{ics_escape(chr(10).join(desc_parts))}")
    if booking.join_link:
        lines.append(f"LOCATION:{ics_escape(booking.join_link)}")
        lines.append(f"URL:{booking.join_link}")  # a URI value, not TEXT: no escaping
    lines += ["STATUS:TENTATIVE" if booking.is_requested else "STATUS:CONFIRMED", "END:VEVENT"]
    return lines


def build_tutor_feed(tutor, bookings):
    """A multi-event VCALENDAR of the tutor's bookings, ready to serve as a
    subscription feed. Cancelled bookings are deleted rows, so simply not
    emitting them makes subscribed calendars drop the event on the next poll."""
    stamp = timezone.now().astimezone(dt_timezone.utc)
    first = (tutor.get_full_name() or tutor.username).split(" ")[0]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//The Green Pencil//Tutor Schedule//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(f'The Green Pencil · Stunden von {first}')}",
        # Hint clients to re-poll hourly so cancellations/reschedules land fast.
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    seen_groups = set()
    for booking in bookings:
        # A group session is one booking per student: one calendar event.
        if booking.group_session:
            if booking.group_session in seen_groups:
                continue
            seen_groups.add(booking.group_session)
        lines += _event_lines(booking, stamp)
    lines.append("END:VCALENDAR")
    return ics_join(lines)
