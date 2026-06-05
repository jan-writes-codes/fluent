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

from unittest import mock

from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from .models import User, Booking, Receipt, ActiveLesson, LessonFile


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
# Admin user management: create/edit must persist + be loggable into
# --------------------------------------------------------------------------- #
class AdminUserManagementTests(FluentDataMixin, TestCase):
    def test_created_and_edited_student_can_log_in(self):
        self.client.force_login(self.admin)
        # 1) create a blank student
        created = self.client.post("/api/users/", data="{}", content_type="application/json").json()
        slug = created["slug"]
        self.assertTrue(User.objects.filter(slug=slug).exists())
        # 2) edit name + login email + password (what the admin editor sends)
        resp = self.client.put(
            f"/api/users/{slug}/",
            data=json.dumps({
                "name": "Jan Heissenberger",
                "email": "jan@fluent.at",
                "password": "geheim123",
                "credits": 2,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        # persisted to the DB
        u = User.objects.get(slug=slug)
        self.assertEqual(u.email, "jan@fluent.at")
        self.assertEqual(u.credits, 2)
        # initials recomputed from the new name -> avatar is correct after reload
        self.assertEqual(u.initials, "JH")
        self.assertEqual(resp.json()["initials"], "JH")
        # 3) the new credentials actually work (the reported bug)
        fresh = Client()
        login = fresh.post(
            "/api/login/",
            data=json.dumps({"email": "jan@fluent.at", "password": "geheim123"}),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["role"], "student")
        self.assertEqual(fresh.get("/").status_code, 200)

    def test_billing_name_change_updates_initials(self):
        self.client.force_login(self.maya)
        self.client.put(
            "/api/users/me/billing/",
            data=json.dumps({"name": "Maya Olsen"}),
            content_type="application/json",
        )
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.initials, "MO")


# --------------------------------------------------------------------------- #
# Authorization (RBAC + object-level / IDOR) and login hardening
# --------------------------------------------------------------------------- #
class AuthorizationTests(FluentDataMixin, TestCase):
    def _post(self, url, body=None):
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json")

    def _put(self, url, body=None):
        return self.client.put(url, data=json.dumps(body or {}), content_type="application/json")

    # ---- anonymous is locked out of every mutating endpoint (401) ----
    def test_anonymous_endpoints_require_auth(self):
        for method, url in [
            ("post", "/api/users/"),
            ("put", "/api/users/maya/"),
            ("post", "/api/credits/maya/"),
            ("post", "/api/availability/"),
            ("post", "/api/custom-times/"),
            ("post", "/api/notes/maya/"),
            ("post", "/api/lessons/maya/"),
            ("put", "/api/settings/"),
            ("post", "/api/bookings/"),
        ]:
            fn = getattr(self, f"_{method}")
            self.assertEqual(fn(url).status_code, 401, f"{method} {url} should be 401 for anonymous")

    # ---- a student is forbidden from privileged endpoints (403) ----
    def test_student_cannot_reach_admin_or_tutor_endpoints(self):
        self.client.force_login(self.maya)
        self.assertEqual(self.client.get("/api/users/").status_code, 403)
        self.assertEqual(self._post("/api/users/").status_code, 403)
        self.assertEqual(self._put("/api/users/ines/", {"name": "Hacked"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/users/ines/").status_code, 403)
        self.assertEqual(self._post("/api/credits/maya/", {"n": 99}).status_code, 403)
        self.assertEqual(self._post("/api/availability/", {"date": "2026-5-1", "time": "10:00"}).status_code, 403)
        self.assertEqual(self._post("/api/custom-times/", {"date": "2026-5-1", "time": "10:00"}).status_code, 403)
        self.assertEqual(self._post("/api/notes/ines/", {"text": "x"}).status_code, 403)
        self.assertEqual(self._post("/api/lessons/ines/", {"lessonId": "a1-1"}).status_code, 403)
        self.assertEqual(self._put("/api/settings/", {"creditPrice": 1}).status_code, 403)

    def test_student_cannot_grant_self_credits(self):
        self.client.force_login(self.maya)
        before = self.maya.credits
        self.assertEqual(self._post("/api/credits/maya/", {"n": 100}).status_code, 403)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, before)

    def test_student_cannot_book_as_another_student(self):
        self.client.force_login(self.maya)
        resp = self._post("/api/bookings/", {
            "studentSlug": "ines", "tutorSlug": "davit", "date": "2026-06-08", "time": "09:30",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Booking.objects.filter(student=self.ines, date=date(2026, 6, 8), time="09:30").exists())

    def test_student_cannot_modify_others_booking_idor(self):
        # self.ines_booking belongs to Ines; Maya must not touch it.
        self.client.force_login(self.maya)
        self.assertEqual(self._put(f"/api/bookings/{self.ines_booking.pk}/", {"time": "08:00"}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/bookings/{self.ines_booking.pk}/").status_code, 403)
        self.assertTrue(Booking.objects.filter(pk=self.ines_booking.pk).exists())

    def test_student_can_manage_own_booking(self):
        self.client.force_login(self.maya)
        mine = Booking.objects.create(student=self.maya, tutor=self.davit, date=date(2026, 6, 8), time="09:30")
        self.assertEqual(self._put(f"/api/bookings/{mine.pk}/", {"time": "10:30", "notes": "hi"}).status_code, 200)
        mine.refresh_from_db()
        self.assertEqual(mine.time, "10:30")
        # tutor-only fields are ignored for students
        self._put(f"/api/bookings/{mine.pk}/", {"tutorNotes": "secret", "callLink": "http://x"})
        mine.refresh_from_db()
        self.assertEqual(mine.tutor_notes, "")
        self.assertEqual(mine.call_link, "")
        self.assertEqual(self.client.delete(f"/api/bookings/{mine.pk}/").status_code, 200)

    # ---- a tutor can run tutor endpoints but not admin ones ----
    def test_tutor_permissions(self):
        self.client.force_login(self.davit)
        self.assertEqual(self._post("/api/credits/maya/", {"n": 1}).status_code, 200)
        self.assertEqual(self._post("/api/notes/maya/", {"text": "great progress"}).status_code, 200)
        self.assertEqual(self._post("/api/availability/", {"date": "2026-5-1", "time": "10:00", "isOpen": False}).status_code, 200)
        # but not admin-only user management / settings
        self.assertEqual(self.client.get("/api/users/").status_code, 403)
        self.assertEqual(self._put("/api/settings/", {"creditPrice": 40}).status_code, 403)

    # ---- admin guards ----
    def test_admin_cannot_delete_self(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.delete("/api/users/admin/").status_code, 400)
        self.assertTrue(User.objects.filter(slug="admin").exists())

    def test_admin_cannot_set_duplicate_email(self):
        self.client.force_login(self.admin)
        resp = self._put("/api/users/ines/", {"email": "maya@fluent.at"})
        self.assertEqual(resp.status_code, 400)
        self.ines.refresh_from_db()
        self.assertNotEqual(self.ines.email, "maya@fluent.at")

    # ---- login doesn't leak which emails exist ----
    def test_login_errors_do_not_enumerate(self):
        unknown = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "ghost@fluent.at", "password": "x"}),
            content_type="application/json",
        )
        wrong = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "maya@fluent.at", "password": "wrong"}),
            content_type="application/json",
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(wrong.status_code, 400)
        # identical message -> can't tell a real account from a fake one
        self.assertEqual(unknown.json()["error"], wrong.json()["error"])


class CsrfTests(FluentDataMixin, TestCase):
    def test_mutating_endpoints_enforce_csrf(self):
        # A logged-in session without a CSRF token must still be rejected.
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.admin)
        resp = c.post("/api/users/", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 403)


# --------------------------------------------------------------------------- #
# Pricing: pack purchases bill the package total, not credits × per-credit rate
# --------------------------------------------------------------------------- #
class PricingReceiptTests(FluentDataMixin, TestCase):
    def _add_credits(self, student_slug, n):
        self.client.force_login(self.davit)  # tutor grants credits
        return self.client.post(
            f"/api/credits/{student_slug}/",
            data=json.dumps({"n": n}),
            content_type="application/json",
        )

    def test_pack_purchase_bills_package_total(self):
        # Default packs: 10 credits = €270 (i.e. €27/credit, not 10 × €30).
        self._add_credits("maya", 10)
        r = Receipt.objects.filter(student=self.maya, credits=10).latest("created_at")
        self.assertEqual(r.unit_price_cents, 27)
        self.assertEqual(r.credits * r.unit_price_cents, 270)  # total shown on receipt

    def test_five_pack_uses_pack_price(self):
        self._add_credits("maya", 5)
        r = Receipt.objects.filter(student=self.maya, credits=5).latest("created_at")
        self.assertEqual(r.credits * r.unit_price_cents, 145)

    def test_non_pack_amount_uses_per_credit_rate(self):
        # 3 credits isn't a pack -> falls back to the €30 per-credit receipts rate.
        self._add_credits("maya", 3)
        r = Receipt.objects.filter(student=self.maya, credits=3).latest("created_at")
        self.assertEqual(r.unit_price_cents, 30)
        self.assertEqual(r.credits * r.unit_price_cents, 90)


# --------------------------------------------------------------------------- #
# Lesson PDF materials: upload (tutor), download (access-controlled), scoping
# --------------------------------------------------------------------------- #
_LESSON_MEDIA = tempfile.mkdtemp(prefix="fluent-test-media-")


@override_settings(MEDIA_ROOT=_LESSON_MEDIA)
class LessonFileTests(FluentDataMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_LESSON_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        ActiveLesson.objects.create(student=self.maya, lesson_id="a1-1")  # ines does NOT have it

    def _pdf(self, name="worksheet.pdf"):
        return SimpleUploadedFile(name, b"%PDF-1.4 test pdf", content_type="application/pdf")

    def _upload(self, lesson="a1-1"):
        return self.client.post(f"/api/lesson-files/{lesson}/", {"file": self._pdf()})

    def test_tutor_uploads_pdf(self):
        self.client.force_login(self.davit)
        resp = self._upload()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "worksheet.pdf")
        self.assertEqual(LessonFile.objects.filter(lesson_id="a1-1").count(), 1)

    def test_non_pdf_rejected(self):
        self.client.force_login(self.davit)
        bad = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        resp = self.client.post("/api/lesson-files/a1-1/", {"file": bad})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(LessonFile.objects.count(), 0)

    def test_oversize_rejected(self):
        self.client.force_login(self.davit)
        with mock.patch("core.views.MAX_LESSON_FILE_BYTES", 4):
            resp = self._upload()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(LessonFile.objects.count(), 0)

    def test_student_cannot_upload_or_delete(self):
        self.client.force_login(self.davit)
        lf_id = self._upload().json()["id"]
        self.client.force_login(self.maya)
        self.assertEqual(self._upload().status_code, 403)
        self.assertEqual(self.client.delete(f"/api/lesson-files/{lf_id}/").status_code, 403)
        self.assertTrue(LessonFile.objects.filter(pk=lf_id).exists())

    def test_download_access_control(self):
        self.client.force_login(self.davit)
        lf_id = self._upload().json()["id"]
        url = f"/api/lesson-files/download/{lf_id}/"
        # maya has a1-1 unlocked -> can download
        self.client.force_login(self.maya)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertIn("attachment", r["Content-Disposition"])
        # ines has NOT unlocked a1-1 -> forbidden
        self.client.force_login(self.ines)
        self.assertEqual(self.client.get(url).status_code, 403)
        # anonymous -> 401
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_payload_scopes_files_to_unlocked_lessons(self):
        self.client.force_login(self.davit)
        self._upload()
        # maya (a1-1 unlocked) receives the file
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get("/").content.decode())
        self.assertIn("a1-1", payload["lessonFiles"])
        self.assertEqual(payload["lessonFiles"]["a1-1"][0]["name"], "worksheet.pdf")
        # ines (locked) does not
        self.client.force_login(self.ines)
        payload = extract_payload(self.client.get("/").content.decode())
        self.assertNotIn("a1-1", payload.get("lessonFiles", {}))

    def test_tutor_deletes_file(self):
        self.client.force_login(self.davit)
        lf_id = self._upload().json()["id"]
        self.assertEqual(self.client.delete(f"/api/lesson-files/{lf_id}/").status_code, 200)
        self.assertFalse(LessonFile.objects.filter(pk=lf_id).exists())


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

    def run_probe(self, user, book=False, tz=None, admin_rename=False, admin_save=False, admin_pricing=False, learning=False):
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
            cmd = [self._node, PROBE, path]
            if book:
                cmd.append("--book")
            if admin_rename:
                cmd.append("--admin-rename")
            if admin_save:
                cmd.append("--admin-save")
            if admin_pricing:
                cmd.append("--admin-pricing")
            if learning:
                cmd.append("--learning")
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


class DomAdminTests(_DomProbeBase):
    def test_avatar_initials_update_live_on_rename(self):
        # Admin editor opens on a user; typing a new name must update the
        # avatar initials immediately (no save / reload).
        r = self.run_probe(self.admin, admin_rename=True)
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertEqual(r["adminRename"]["avatarInitials"], "JH")
        self.assertEqual(r["adminRename"]["headingName"], "Jan Heissenberger")

    def test_admin_add_and_save_persist_to_server(self):
        # The reported bug: editing a student in the admin UI never hit the
        # server, so the account couldn't be logged into. Drive the real UI and
        # assert both the create (POST) and the edit (PUT) are sent.
        r = self.run_probe(self.admin, admin_save=True)
        self.assertEqual(r["initErrors"], [], "init must not throw")
        s = r["adminSave"]
        self.assertTrue(s["createPosted"], "adding a student must POST /api/users/")
        self.assertIsNotNone(s["editPut"], "saving must PUT /api/users/<slug>/")
        self.assertEqual(s["editPut"]["body"]["email"], "jan@fluent.at")
        self.assertEqual(s["editPut"]["body"]["name"], "Jan Heissenberger")
        self.assertEqual(s["editPut"]["body"]["password"], "geheim123")


class DomPricingTests(_DomProbeBase):
    def test_per_session_is_readonly_and_auto_derived(self):
        r = self.run_probe(self.admin, admin_pricing=True)
        self.assertEqual(r["initErrors"], [])
        p = r["adminPricing"]
        self.assertTrue(p["readonly"], "per-session field must not be editable")
        # 1-credit pack priced at €100 -> €100 / session
        self.assertEqual(p["eachAfterPrice"], "€100 / session")
        # credits = 0 -> blank, never NaN/Infinity (ZeroDivision -> none)
        self.assertEqual(p["eachAfterZeroCredits"], "")


@override_settings(MEDIA_ROOT=_LESSON_MEDIA)
class DomLessonTests(_DomProbeBase):
    def test_student_sees_real_download_link(self):
        ActiveLesson.objects.create(student=self.maya, lesson_id="a1-1")
        lf = LessonFile.objects.create(
            lesson_id="a1-1",
            file=SimpleUploadedFile("worksheet.pdf", b"%PDF-1.4", content_type="application/pdf"),
            original_name="worksheet.pdf", uploaded_by=self.davit,
        )
        r = self.run_probe(self.maya, learning=True)
        self.assertEqual(r["initErrors"], [])
        self.assertIn(f"/api/lesson-files/download/{lf.pk}/", r["learning"]["fileLinks"])


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
