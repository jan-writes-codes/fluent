"""
Automated regression tests for the bugs fixed on this branch.

Two layers:

* Backend (Django ``Client``) tests cover everything observable server-side:
  the standalone /login/ page + auto-redirects, booking persistence and the
  role-scoped payload (a student must never receive another student's identity),
  and the ``data-role`` attribute that CSS uses to gate the nav tabs.

* Frontend (``FrontendDomTests``) tests load the *rendered* page into jsdom and
  run its real init script — the only way to catch the purely client-side
  failures: an init exception that leaves every tab visible, the header identity
  getting stuck on the static "Maya Karlsson" default, and the booking POST
  being swallowed by a render crash / shifted by a timezone bug. These require
  Node + jsdom (``cd tests/frontend && npm install``); if either is missing the
  whole class is skipped with an explanatory message rather than failing.

Map of bug -> guarding test:
  * student sees tutor/admin tabs ....... DomRoleTests.test_tabs_*
  * login is its own page w/ redirects ... LoginPageTests.*
  * bookings don't sync between users .... BookingPersistenceTests.*  +  DomBookingTests.test_booking_persists_with_local_date
  * identity always shows Maya ........... DomRoleTests.test_identity_*
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, timedelta

from django.test import TestCase, Client
from django.urls import reverse

from .models import User, Booking


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #
def make_user(slug, role, **extra):
    defaults = dict(
        username=slug,
        email=f"{slug}@fluent.at",
        role=role,
        slug=slug,
        initials=slug[:2].upper(),
    )
    defaults.update(extra)
    user = User.objects.create_user(password="password", **defaults)
    return user


def current_week_monday():
    """Monday of the week containing today (so fixtures land in the default view)."""
    today = date.today()
    return today - timedelta(days=today.weekday())


DJANGO_DATA_RE = re.compile(r"window\.DJANGO_DATA = (\{.*?\});", re.S)
DATA_ROLE_RE = re.compile(r'<div class="app" data-role="([^"]*)"')


def extract_payload(html):
    m = DJANGO_DATA_RE.search(html)
    assert m, "DJANGO_DATA not found in rendered page"
    return json.loads(m.group(1))


def extract_data_role(html):
    m = DATA_ROLE_RE.search(html)
    return m.group(1) if m else None


class FluentDataMixin:
    """Creates a tutor, two students (one with a booking) and an admin."""

    def setUp(self):
        self.client = Client()
        self.davit = make_user(
            "davit", "tutor", first_name="Davit", last_name="Petrosyan", initials="DV"
        )
        self.maya = make_user(
            "maya", "student", first_name="Maya", last_name="Karlsson",
            initials="MK", credits=8,
        )
        self.ines = make_user(
            "ines", "student", first_name="Ines", last_name="Reyes",
            initials="IR", credits=3,
        )
        self.admin = make_user(
            "admin", "admin", first_name="Studio", last_name="Admin", initials="AD"
        )
        # A booking owned by *ines* so that maya's payload contains an anonymized
        # "__blocked__" entry — the exact data shape that used to crash init.
        self.ines_booking = Booking.objects.create(
            student=self.ines, tutor=self.davit,
            date=current_week_monday(), time="10:00", title="Ines session",
        )


# --------------------------------------------------------------------------- #
# Login page + auto-redirect
# --------------------------------------------------------------------------- #
class LoginPageTests(FluentDataMixin, TestCase):
    def test_anonymous_app_redirects_to_login(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], reverse("login"))

    def test_login_page_renders_standalone(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="authForm"')
        self.assertContains(resp, "Sign in")

    def test_authenticated_login_redirects_to_app(self):
        self.client.force_login(self.maya)
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], reverse("app"))

    def test_authenticated_app_renders(self):
        self.client.force_login(self.maya)
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_api_login_success_then_logout(self):
        resp = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "davit@fluent.at", "password": "password"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["role"], "tutor")
        # session is live: app renders
        self.assertEqual(self.client.get("/").status_code, 200)
        # logout, then app bounces to login again
        self.client.post("/api/logout/")
        self.assertEqual(self.client.get("/").headers["Location"], reverse("login"))

    def test_api_login_bad_password(self):
        resp = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "maya@fluent.at", "password": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_api_login_unknown_email(self):
        resp = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "ghost@fluent.at", "password": "password"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


# --------------------------------------------------------------------------- #
# Booking persistence + cross-user visibility  (Bug A, backend half)
# --------------------------------------------------------------------------- #
class BookingPersistenceTests(FluentDataMixin, TestCase):
    def _book(self, student_slug, tutor_slug, d, t):
        self.client.force_login(getattr(self, student_slug))
        return self.client.post(
            "/api/bookings/",
            data=json.dumps({
                "studentSlug": student_slug, "tutorSlug": tutor_slug,
                "date": d, "time": t, "title": "English session",
            }),
            content_type="application/json",
        )

    def test_booking_is_persisted_with_exact_date(self):
        resp = self._book("maya", "davit", "2026-06-08", "09:30")
        self.assertEqual(resp.status_code, 200)
        b = Booking.objects.get(pk=resp.json()["pk"])
        self.assertEqual(b.student, self.maya)
        self.assertEqual(b.date, date(2026, 6, 8))   # no timezone shift server-side
        self.assertEqual(b.time, "09:30")

    def test_tutor_sees_students_booking(self):
        self._book("maya", "davit", "2026-06-08", "09:30")
        self.client.force_login(self.davit)
        payload = extract_payload(self.client.get("/").content.decode())
        match = [
            b for b in payload["bookings"]
            if b["studentId"] == "maya" and b["date"] == "2026-06-08" and b["time"] == "09:30"
        ]
        self.assertEqual(len(match), 1, "tutor should see the student's booking by identity")

    def test_other_student_sees_booking_anonymized(self):
        self._book("maya", "davit", "2026-06-08", "09:30")
        self.client.force_login(self.ines)
        payload = extract_payload(self.client.get("/").content.decode())
        # The slot is blocked for ines...
        blocked = [
            b for b in payload["bookings"]
            if b["date"] == "2026-06-08" and b["time"] == "09:30"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["studentId"], "__blocked__")
        self.assertEqual(blocked[0]["title"], "")          # no leaked details
        # ...and maya's real identity is never sent to ines.
        self.assertFalse(any(b["studentId"] == "maya" for b in payload["bookings"]))

    def test_tutor_assigns_student_persists(self):
        # Davit books Ines -> Ines must see it on next load.
        self.client.force_login(self.davit)
        resp = self.client.post(
            "/api/bookings/",
            data=json.dumps({
                "studentSlug": "ines", "tutorSlug": "davit",
                "date": "2026-06-09", "time": "14:30", "title": "English session",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.client.force_login(self.ines)
        payload = extract_payload(self.client.get("/").content.decode())
        match = [
            b for b in payload["bookings"]
            if b["studentId"] == "ines" and b["date"] == "2026-06-09" and b["time"] == "14:30"
        ]
        self.assertEqual(len(match), 1)


# --------------------------------------------------------------------------- #
# Role-scoped payload + data-role attribute
# --------------------------------------------------------------------------- #
class RoleScopingTests(FluentDataMixin, TestCase):
    def _payload_for(self, user):
        self.client.force_login(user)
        html = self.client.get("/").content.decode()
        return extract_payload(html), extract_data_role(html)

    def test_student_payload_is_self_only(self):
        payload, data_role = self._payload_for(self.maya)
        self.assertEqual(data_role, "student")
        self.assertEqual(payload["role"], "student")
        self.assertEqual(payload["currentUserId"], "maya")
        slugs = {s["slug"] for s in payload["students"]}
        self.assertEqual(slugs, {"maya"}, "a student must not receive other students")

    def test_tutor_payload_has_all_students(self):
        payload, data_role = self._payload_for(self.davit)
        self.assertEqual(data_role, "tutor")
        slugs = {s["slug"] for s in payload["students"]}
        self.assertEqual(slugs, {"maya", "ines"})

    def test_admin_data_role(self):
        _payload, data_role = self._payload_for(self.admin)
        self.assertEqual(data_role, "admin")


# --------------------------------------------------------------------------- #
# Frontend (jsdom) tests — run the real init script in a headless DOM
# --------------------------------------------------------------------------- #
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "tests", "frontend")
PROBE = os.path.join(FRONTEND_DIR, "dom_probe.js")


def _node_bin():
    return shutil.which("node")


def _jsdom_installed():
    return os.path.isdir(os.path.join(FRONTEND_DIR, "node_modules", "jsdom"))


class _DomProbeBase(FluentDataMixin, TestCase):
    """Renders a page for a user and runs the jsdom probe against it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._node = _node_bin()
        cls._has_jsdom = _jsdom_installed()

    def _skip_if_unavailable(self):
        if not self._node:
            self.skipTest("node not found on PATH — skipping jsdom frontend tests")
        if not self._has_jsdom:
            self.skipTest(
                "jsdom not installed — run `cd tests/frontend && npm install` "
                "to enable frontend tests"
            )

    def run_probe(self, user, book=False, tz=None):
        self._skip_if_unavailable()
        self.client.force_login(user)
        html = self.client.get("/").content.decode()
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
            fh.write(html)
            path = fh.name
        try:
            env = dict(os.environ)
            if tz:
                env["TZ"] = tz
            cmd = [self._node, PROBE, path] + (["--book"] if book else [])
            out = subprocess.run(
                cmd, capture_output=True, text=True, env=env, timeout=60
            )
            self.assertEqual(
                out.returncode, 0,
                msg=f"probe failed: {out.stderr or out.stdout}",
            )
            return json.loads(out.stdout)
        finally:
            os.unlink(path)


class DomRoleTests(_DomProbeBase):
    EXPECTED = {
        "maya":  ("Maya Karlsson", {"book", "account", "games", "files"}),
        "davit": ("Davit Petrosyan", {"teacher", "students"}),
        "admin": ("Studio Admin", {"admin"}),
    }

    def _check(self, user, expected_name, expected_tabs):
        r = self.run_probe(user)
        self.assertEqual(r["initErrors"], [], "init must not throw")
        # identity (Bug B)
        self.assertEqual(r["identity"]["name"], expected_name)
        self.assertEqual(r["identity"]["mail"], f"{user.slug}@fluent.at")
        # tab gating (the "student sees everything" bug)
        visible = {v for v, disp in r["tabs"].items() if disp != "none"}
        self.assertEqual(visible, expected_tabs)

    def test_identity_and_tabs_student(self):
        self._check(self.maya, *self.EXPECTED["maya"])

    def test_identity_and_tabs_tutor(self):
        self._check(self.davit, *self.EXPECTED["davit"])

    def test_identity_and_tabs_admin(self):
        self._check(self.admin, *self.EXPECTED["admin"])


class DomBookingTests(_DomProbeBase):
    def test_booking_persists_with_local_date(self):
        # Run under a large positive UTC offset: the old toISOString() code
        # shifted a local-midnight date back a day here.
        r = self.run_probe(self.maya, book=True, tz="Asia/Tokyo")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        b = r["booking"]
        self.assertTrue(b.get("posted"), f"booking POST was not sent: {b}")
        self.assertRegex(b["postedDate"], r"^\d{4}-\d{2}-\d{2}$")
        # The POSTed day must match the day the UI showed the user (no tz shift).
        self.assertIsNotNone(b["panelDay"])
        self.assertEqual(
            b["postedDay"], b["panelDay"],
            msg=f"booking date shifted: sent {b['postedDate']} but UI showed {b['panelDate']}",
        )
