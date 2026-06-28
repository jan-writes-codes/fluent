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

from .models import (
    User, Booking, Receipt, CreditTransaction, ActiveLesson, LessonFile,
)


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
    def test_landing_page_is_public_home(self):
        # The marketing landing page is the site's front door — public, at "/".
        resp = self.client.get(reverse("landing"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(reverse("landing"), "/")
        self.assertContains(resp, "The Green Pencil")
        # Booking CTAs funnel visitors into the public intro-booking calendar,
        # with sign-in as a separate entry point.
        self.assertContains(resp, 'href="/intro/"')
        self.assertContains(resp, 'href="/login/"')

    def test_anonymous_app_redirects_to_login(self):
        resp = self.client.get(reverse("app"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], reverse("login"))

    def test_login_page_renders_standalone(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="authForm"')
        self.assertContains(resp, "Anmelden")

    def test_authenticated_login_redirects_to_app(self):
        self.client.force_login(self.maya)
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], reverse("app"))

    def test_authenticated_app_renders(self):
        self.client.force_login(self.maya)
        resp = self.client.get(reverse("app"))
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
        self.assertEqual(self.client.get(reverse("app")).status_code, 200)
        # logout, then app bounces to login again
        self.client.post("/api/logout/")
        self.assertEqual(self.client.get(reverse("app")).headers["Location"], reverse("login"))

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
# Legal pages + account-creation hardening  (GDPR)
# --------------------------------------------------------------------------- #
class GdprComplianceTests(FluentDataMixin, TestCase):
    def test_legal_pages_are_public(self):
        for name in ("impressum", "datenschutz"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)

    def test_new_student_gets_unique_non_default_password(self):
        # A guessable shared default ("password") on every new account is a data-
        # protection risk. Each student must get a unique, working credential.
        self.client.force_login(self.admin)
        resp = self.client.post("/api/users/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        temp = data.get("tempPassword")
        self.assertTrue(temp, "create response must surface the generated password once")
        self.assertNotEqual(temp, "password")
        created = User.objects.get(slug=data["slug"])
        self.assertFalse(created.check_password("password"))
        self.assertTrue(created.check_password(temp))


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
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        match = [
            b for b in payload["bookings"]
            if b["studentId"] == "maya" and b["date"] == "2026-06-08" and b["time"] == "09:30"
        ]
        self.assertEqual(len(match), 1, "tutor should see the student's booking by identity")

    def test_other_student_sees_booking_anonymized(self):
        self._book("maya", "davit", "2026-06-08", "09:30")
        self.client.force_login(self.ines)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
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
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
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
        html = self.client.get(reverse("app")).content.decode()
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
        self.assertEqual(fresh.get(reverse("app")).status_code, 200)

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
# Multi-tutor: admin can create/manage several tutors; their calendars are
# isolated from one another.
# --------------------------------------------------------------------------- #
class MultiTutorTests(FluentDataMixin, TestCase):
    def test_admin_creates_tutor_who_can_log_in(self):
        self.client.force_login(self.admin)
        created = self.client.post(
            "/api/users/",
            data=json.dumps({"role": "tutor", "name": "Lena Bauer"}),
            content_type="application/json",
        ).json()
        self.assertTrue(created["slug"].startswith("tut"))
        self.assertEqual(created["role"], "tutor")
        self.assertEqual(created["initials"], "LB")
        self.assertIn("tempPassword", created)
        # the temp password actually logs in, as a tutor
        fresh = Client()
        login = fresh.post(
            "/api/login/",
            data=json.dumps({"email": created["email"], "password": created["tempPassword"]}),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["role"], "tutor")

    def test_edited_tutor_credentials_work(self):
        """The reported bug: change a tutor's login, then sign in with it."""
        self.client.force_login(self.admin)
        resp = self.client.put(
            f"/api/users/{self.davit.slug}/",
            data=json.dumps({
                "name": "Davit Petrosyan",
                "email": "new.davit@fluent.at",
                "password": "totallynew99",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        fresh = Client()
        login = fresh.post(
            "/api/login/",
            data=json.dumps({"email": "new.davit@fluent.at", "password": "totallynew99"}),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["role"], "tutor")

    def test_invalid_role_is_rejected(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            "/api/users/",
            data=json.dumps({"role": "admin"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_availability_is_isolated_per_tutor(self):
        other = make_user("lena", "tutor", first_name="Lena", last_name="Bauer", initials="LB")
        self.client.force_login(self.admin)
        # admin opens the SAME slot for davit but closes it for lena
        for slug, is_open in ((self.davit.slug, True), (other.slug, False)):
            r = self.client.post(
                "/api/availability/",
                data=json.dumps({"date": "2026-5-2", "time": "11:00", "isOpen": is_open, "tutorSlug": slug}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 200)
        # the payload keys each tutor's overrides separately — no collision
        html = self.client.get(reverse("app")).content.decode()
        avail = extract_payload(html)["availability"]
        self.assertEqual(avail[self.davit.slug]["2026-5-2|11:00"], True)
        self.assertEqual(avail[other.slug]["2026-5-2|11:00"], False)

    def test_admin_custom_time_requires_known_tutor(self):
        self.client.force_login(self.admin)
        r = self.client.post(
            "/api/custom-times/",
            data=json.dumps({"date": "2026-5-2", "time": "07:30", "tutorSlug": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)


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

    def test_disallowed_type_rejected(self):
        self.client.force_login(self.davit)
        bad = SimpleUploadedFile("malware.exe", b"MZ", content_type="application/octet-stream")
        resp = self.client.post("/api/lesson-files/a1-1/", {"file": bad})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(LessonFile.objects.count(), 0)

    def test_non_pdf_materials_allowed_with_kind(self):
        self.client.force_login(self.davit)
        mp3 = SimpleUploadedFile("listening.mp3", b"ID3 audio", content_type="audio/mpeg")
        resp = self.client.post("/api/lesson-files/a1-1/", {"file": mp3})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["kind"], "audio")
        self.assertEqual(resp.json()["ext"], "MP3")
        # served with the right content-type
        self.client.force_login(self.maya)
        dl = self.client.get(resp.json()["url"])
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl["Content-Type"], "audio/mpeg")

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
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertIn("a1-1", payload["lessonFiles"])
        self.assertEqual(payload["lessonFiles"]["a1-1"][0]["name"], "worksheet.pdf")
        # ines (locked) does not
        self.client.force_login(self.ines)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
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
INTRO_PROBE = os.path.join(FRONTEND_DIR, "intro_probe.js")


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

    def run_probe(self, user, book=False, tz=None, admin_rename=False, admin_save=False, admin_pricing=False, learning=False, preview=False, admin_add_tutor=False, buy=False):
        self._skip_if_unavailable()
        self.client.force_login(user)
        html = self.client.get(reverse("app")).content.decode()
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
            if admin_add_tutor:
                cmd.append("--admin-add-tutor")
            if admin_pricing:
                cmd.append("--admin-pricing")
            if learning:
                cmd.append("--learning")
            if preview:
                cmd.append("--preview")
            if buy:
                cmd.append("--buy")
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
        # A newly added student must open in the *student* editor — not the tutor
        # mask (the bug: the locally-mirrored account had no role, so it rendered
        # as a tutor with no credits/billing fields).
        self.assertEqual(s["editorRole"], "Schüler")
        self.assertTrue(s["hasCreditsField"], "student editor must show the Credits field")
        self.assertTrue(s["hasRemoveStudent"], "student editor must offer 'Schüler entfernen'")

    def test_admin_add_tutor_button_creates_and_selects_tutor(self):
        # The new "+ Tutor hinzufügen" button: POST must carry role:tutor, and the
        # created tutor must become the selected, editable account (temp password
        # shown so the admin can hand it over) with a "remove tutor" control.
        r = self.run_probe(self.admin, admin_add_tutor=True)
        self.assertEqual(r["initErrors"], [], "init must not throw")
        a = r["adminAddTutor"]
        self.assertEqual(a["postedRole"], "tutor", "must POST role:tutor")
        self.assertEqual(a["editorPassword"], "tmp-tutor-pw", "temp password shown in editor")
        self.assertTrue(a["hasRemoveTutor"], "tutor editor must offer removal")


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

    def test_tutor_preview_shows_student_materials(self):
        ActiveLesson.objects.create(student=self.maya, lesson_id="a1-1")
        lf = LessonFile.objects.create(
            lesson_id="a1-1",
            file=SimpleUploadedFile("worksheet.pdf", b"%PDF-1.4", content_type="application/pdf"),
            original_name="worksheet.pdf", uploaded_by=self.davit,
        )
        r = self.run_probe(self.davit, preview=True)
        self.assertEqual(r["initErrors"], [])
        self.assertIn(f"/api/lesson-files/download/{lf.pk}/", r["preview"]["fileLinks"])


class DomBookingTests(_DomProbeBase):
    def setUp(self):
        super().setUp()
        # Availability is opt-in now (no seeded defaults). The app pins its
        # calendar "today" to Mon 1 Jun 2026, so open a few of Davit's slots in
        # that demo week for the booking flow to have something to click.
        from core.models import AvailabilityOverride
        for day in range(1, 6):  # Mon–Fri, 1–5 Jun 2026
            for time in ("09:00", "10:00", "11:00", "14:00"):
                AvailabilityOverride.objects.create(
                    tutor=self.davit, date=date(2026, 6, day), time=time, is_open=True
                )

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


# --------------------------------------------------------------------------- #
# Immutable history + GDPR "anonymise & keep" on account deletion
# --------------------------------------------------------------------------- #
class HistoryRetentionTests(FluentDataMixin, TestCase):
    """Purchase/lesson history must never be altered when an account is deleted:
    the financial records survive (statutory retention) while the personal account
    is erased (GDPR). The records carry frozen identity/billing snapshots so they
    stay readable verbatim after the FK is detached."""

    def _grant(self, n=5):
        # Tutor grants credits -> creates a Receipt + CreditTransaction.
        self.client.force_login(self.davit)
        resp = self.client.post(
            f"/api/credits/{self.maya.slug}/",
            data=json.dumps({"n": n}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_deleting_student_keeps_receipt_with_frozen_snapshot(self):
        self.maya.billing_name = "Maya Karlsson"
        self.maya.billing_line1 = "Hauptstraße 1"
        self.maya.billing_postcode = "1010"
        self.maya.billing_city = "Wien"
        self.maya.save()
        self._grant(5)
        receipt = Receipt.objects.get(student=self.maya)
        # Snapshot is captured at issue time.
        self.assertEqual(receipt.student_slug, "maya")
        self.assertEqual(receipt.student_name, "Maya Karlsson")
        self.assertEqual(receipt.billing_city, "Wien")

        # Admin deletes the student.
        self.client.force_login(self.admin)
        resp = self.client.delete(f"/api/users/{self.maya.slug}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(slug="maya").exists())

        # Receipt is retained, detached (FK NULL), snapshot intact.
        receipt.refresh_from_db()
        self.assertIsNone(receipt.student_id)
        self.assertEqual(receipt.student_slug, "maya")
        self.assertEqual(receipt.student_name, "Maya Karlsson")
        self.assertEqual(receipt.billing_postcode, "1010")

    def test_deleting_student_keeps_transactions_and_bookings(self):
        self._grant(3)
        # A held lesson (booking) for maya.
        booking = Booking.objects.create(
            student=self.maya, tutor=self.davit,
            date=current_week_monday(), time="12:00", title="Maya session",
        )
        self.assertEqual(booking.student_slug, "maya")
        self.assertEqual(booking.tutor_slug, "davit")

        txn = CreditTransaction.objects.get(student=self.maya)
        self.assertEqual(txn.student_slug, "maya")

        self.client.force_login(self.admin)
        self.client.delete(f"/api/users/{self.maya.slug}/")

        booking.refresh_from_db()
        txn.refresh_from_db()
        self.assertIsNone(booking.student_id)
        self.assertEqual(booking.student_slug, "maya")
        self.assertEqual(booking.tutor_slug, "davit")  # tutor still there
        self.assertIsNone(txn.student_id)
        self.assertEqual(txn.student_slug, "maya")

    def test_deleting_tutor_keeps_booking_history(self):
        booking = Booking.objects.create(
            student=self.maya, tutor=self.davit,
            date=current_week_monday(), time="13:00", title="Past session",
        )
        self.client.force_login(self.admin)
        self.client.delete(f"/api/users/{self.davit.slug}/")
        booking.refresh_from_db()
        self.assertIsNone(booking.tutor_id)
        self.assertEqual(booking.tutor_slug, "davit")
        self.assertEqual(booking.tutor_name, "Davit Petrosyan")

    def test_receipt_payload_reads_from_snapshot_after_deletion(self):
        self._grant(2)
        self.client.force_login(self.admin)
        self.client.delete(f"/api/users/{self.maya.slug}/")
        # Admin loads the app: the orphaned receipt must still serialize.
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        receipts = [r for r in payload["receipts"] if r["studentId"] == "maya"]
        self.assertTrue(receipts, "retained receipt must still appear for admin")
        self.assertEqual(receipts[0]["studentName"], "Maya Karlsson")

    def test_receipt_is_frozen_against_later_billing_edits(self):
        self.maya.billing_city = "Wien"
        self.maya.save()
        self._grant(1)
        receipt = Receipt.objects.get(student=self.maya)
        # Student later moves: the issued receipt must not change.
        self.maya.billing_city = "Graz"
        self.maya.save()
        receipt.refresh_from_db()
        self.assertEqual(receipt.billing_city, "Wien")


# --------------------------------------------------------------------------- #
# Stripe self-checkout
# --------------------------------------------------------------------------- #
class StripeCheckoutTests(FluentDataMixin, TestCase):
    def test_checkout_disabled_without_key(self):
        # No STRIPE_SECRET_KEY configured (default): endpoint reports disabled.
        self.client.force_login(self.maya)
        resp = self.client.post(
            "/api/checkout/", data=json.dumps({"n": 5}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 503)

    def test_payload_reports_stripe_disabled_by_default(self):
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertIn("stripe", payload)
        self.assertFalse(payload["stripe"]["enabled"])

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_PUBLISHABLE_KEY="pk_test_x")
    def test_checkout_creates_session(self):
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.create.return_value = mock.Mock(
                url="https://checkout.stripe.test/cs_1", id="cs_1"
            )
            resp = self.client.post(
                "/api/checkout/", data=json.dumps({"n": 5}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["url"], "https://checkout.stripe.test/cs_1")
        # Price is server-authoritative: 5-pack is €145 -> 14500 cents, qty 1.
        _, kwargs = st.checkout.Session.create.call_args
        self.assertEqual(kwargs["line_items"][0]["price_data"]["unit_amount"], 14500)
        self.assertEqual(kwargs["metadata"], {"student_slug": "maya", "credits": "5"})

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_only_students_can_checkout(self):
        self.client.force_login(self.davit)  # tutor
        resp = self.client.post(
            "/api/checkout/", data=json.dumps({"n": 5}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_confirm_credits_student_once(self):
        start = self.maya.credits
        session = {
            "id": "cs_paid_1", "payment_status": "paid",
            "metadata": {"student_slug": "maya", "credits": "5"},
        }
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.retrieve.return_value = session
            r1 = self.client.post(
                "/api/checkout/confirm/", data=json.dumps({"sessionId": "cs_paid_1"}),
                content_type="application/json",
            )
            # Confirm again: must be idempotent (webhook + redirect can both fire).
            r2 = self.client.post(
                "/api/checkout/confirm/", data=json.dumps({"sessionId": "cs_paid_1"}),
                content_type="application/json",
            )
        self.assertTrue(r1.json()["paid"])
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start + 5)
        self.assertEqual(Receipt.objects.filter(stripe_session_id="cs_paid_1").count(), 1)
        self.assertTrue(r2.json()["paid"])  # second call returns the same receipt

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_confirm_rejects_another_students_session(self):
        session = {
            "id": "cs_x", "payment_status": "paid",
            "metadata": {"student_slug": "ines", "credits": "5"},
        }
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.retrieve.return_value = session
            resp = self.client.post(
                "/api/checkout/confirm/", data=json.dumps({"sessionId": "cs_x"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Receipt.objects.filter(stripe_session_id="cs_x").count(), 0)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")  # no webhook secret -> unverified JSON
    def test_webhook_credits_idempotently(self):
        start = self.maya.credits
        event = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_wh_1", "payment_status": "paid",
                "metadata": {"student_slug": "maya", "credits": "10"},
            }},
        })
        self.client.post("/api/stripe/webhook/", data=event, content_type="application/json")
        self.client.post("/api/stripe/webhook/", data=event, content_type="application/json")
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start + 10)
        self.assertEqual(Receipt.objects.filter(stripe_session_id="cs_wh_1").count(), 1)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_webhook_ignores_unpaid_session(self):
        start = self.maya.credits
        event = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_unpaid", "payment_status": "unpaid",
                "metadata": {"student_slug": "maya", "credits": "5"},
            }},
        })
        self.client.post("/api/stripe/webhook/", data=event, content_type="application/json")
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start)
        self.assertEqual(Receipt.objects.filter(stripe_session_id="cs_unpaid").count(), 0)


# --------------------------------------------------------------------------- #
# Buy-credits modal: tutor choice by e-mail (DOM)
# --------------------------------------------------------------------------- #
class DomBuyModalTests(_DomProbeBase):
    def test_single_tutor_mail_targets_that_tutor(self):
        # Only davit exists: no picker, and the e-mail link targets his address.
        r = self.run_probe(self.maya, buy=True)
        self.assertEqual(r["initErrors"], [])
        b = r["buy"]
        self.assertFalse(b["hasTutorSelect"], "one tutor needs no picker")
        self.assertTrue(b["mailHref"].startswith("mailto:davit@fluent.at"),
                        f"mail link must target the tutor: {b['mailHref']}")
        self.assertEqual(b["contactEmail"], "davit@fluent.at")

    def test_multiple_tutors_offer_choice_by_email(self):
        # A second tutor in the system must appear in the picker, listed by e-mail,
        # and the contact e-mail must be a real tutor address (not a hard-coded one).
        make_user("berta", "tutor", first_name="Berta", last_name="Klein", initials="BK")
        r = self.run_probe(self.maya, buy=True)
        self.assertEqual(r["initErrors"], [])
        b = r["buy"]
        self.assertTrue(b["hasTutorSelect"], "two tutors must produce a picker")
        joined = " ".join(b["selectOptions"])
        self.assertIn("davit@fluent.at", joined)
        self.assertIn("berta@fluent.at", joined)
        self.assertIn(b["contactEmail"], {"davit@fluent.at", "berta@fluent.at"})
        self.assertTrue(b["mailHref"].startswith("mailto:"))
        self.assertIn(b["contactEmail"], b["mailHref"])


# --------------------------------------------------------------------------- #
# Public intro-session booking (anonymous, free, no account)
# --------------------------------------------------------------------------- #
class IntroBookingTests(FluentDataMixin, TestCase):
    def _jskey(self, d):
        # Frontend/​server date key: 0-indexed month, "YYYY-M-D".
        return f"{d.year}-{d.month - 1}-{d.day}"

    def _future_date(self, days=3):
        return date.today() + timedelta(days=days)

    def _post(self, **over):
        d = over.pop("date", self._future_date())
        body = {
            "tutorSlug": "davit", "date": self._jskey(d),
            "time": "14:00", "name": "Lena Gast", "email": "lena@example.at",
        }
        body.update(over)
        return self.client.post(
            "/api/intro-bookings/", data=json.dumps(body),
            content_type="application/json",
        )

    def test_intro_page_is_public(self):
        resp = self.client.get(reverse("intro"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Schnupperstunde")
        # The public payload carries tutors but no student PII.
        self.assertContains(resp, "davit")

    def test_landing_book_cta_points_to_intro_not_app(self):
        html = self.client.get(reverse("landing")).content.decode()
        self.assertIn('href="/intro/"', html)
        # A separate sign-in entry point exists.
        self.assertIn('href="/login/"', html)

    def test_guest_books_free_intro(self):
        start_bookings = Booking.objects.count()
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(Booking.objects.count(), start_bookings + 1)
        b = Booking.objects.latest("id")
        self.assertTrue(b.is_intro)
        self.assertIsNone(b.student_id)       # no account
        self.assertEqual(b.tutor.slug, "davit")
        self.assertEqual(b.guest_name, "Lena Gast")
        self.assertEqual(b.guest_email, "lena@example.at")
        self.assertEqual(b.title, "Schnupperstunde (Intro)")

    def test_intro_does_not_touch_credits(self):
        # A free intro must never create a receipt or credit transaction.
        self._post()
        self.assertEqual(Receipt.objects.count(), 0)
        self.assertEqual(CreditTransaction.objects.count(), 0)

    def test_one_intro_per_email(self):
        self.assertEqual(self._post(time="14:00").status_code, 200)
        # Same e-mail, different slot -> rejected.
        again = self._post(time="15:00")
        self.assertEqual(again.status_code, 409)

    def test_cannot_book_taken_slot(self):
        self.assertEqual(self._post(email="a@example.at", time="14:00").status_code, 200)
        # Different guest, same slot -> taken.
        taken = self._post(email="b@example.at", time="14:00")
        self.assertEqual(taken.status_code, 409)

    def test_invalid_email_rejected(self):
        self.assertEqual(self._post(email="not-an-email").status_code, 400)

    def test_unknown_tutor_rejected(self):
        self.assertEqual(self._post(tutorSlug="ghost").status_code, 400)

    def test_past_date_rejected(self):
        resp = self._post(date=date.today() - timedelta(days=1))
        self.assertEqual(resp.status_code, 400)

    def test_closed_slot_rejected(self):
        from core.models import AvailabilityOverride
        d = self._future_date(4)
        AvailabilityOverride.objects.create(
            tutor=self.davit, date=d, time="11:00", is_open=False
        )
        resp = self._post(date=d, time="11:00")
        self.assertEqual(resp.status_code, 409)

    def test_intro_shows_in_tutor_payload_as_guest(self):
        self._post()
        self.client.force_login(self.davit)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        intro = [b for b in payload["bookings"] if b.get("isIntro")]
        self.assertEqual(len(intro), 1)
        self.assertEqual(intro[0]["guestName"], "Lena Gast")
        self.assertEqual(intro[0]["studentId"], "intro")

    def test_student_sees_intro_slot_anonymized(self):
        # A student must not see the guest's identity — just a blocked slot.
        self._post()
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        intro_like = [b for b in payload["bookings"]
                      if b["time"] == "14:00" and b["studentId"] == "__blocked__"]
        self.assertTrue(intro_like, "intro slot must reach the student as a blocker")
        # No guest identity leaks to the student payload.
        self.assertNotIn("Lena", json.dumps(payload))


# --------------------------------------------------------------------------- #
# Public intro calendar — client-side availability + opening-week behaviour
# (jsdom)
#
# Two bugs guarded here:
#   1. Availability used to be a pseudo-random "seeded" default, so EVERY tutor —
#      including a brand-new one — appeared to have open slots they never set.
#      Availability is now opt-in: a tutor starts with a blank calendar and only
#      explicit overrides (or hand-added custom times) open slots.
#   2. The calendar always opened on the current Mon–Fri week, which on a weekend
#      is entirely in the past, so a visitor landed on a blank calendar even when
#      the tutor had upcoming availability. It now opens on the first upcoming
#      week that actually has a bookable slot.
#
# These run the *real* intro init script with a pinned clock so the behaviour is
# deterministic regardless of the day the suite runs.
# --------------------------------------------------------------------------- #
class IntroCalendarFrontendTests(FluentDataMixin, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._node = _node_bin()
        cls._has_jsdom = _jsdom_installed()

    def _open_davit(self, *days):
        """Mark Davit available at 10:00 & 14:00 on the given 2026 June/July days.
        ``days`` are (month, day) tuples."""
        from core.models import AvailabilityOverride
        for month, day in days:
            for time in ("10:00", "14:00"):
                AvailabilityOverride.objects.create(
                    tutor=self.davit, date=date(2026, month, day), time=time, is_open=True
                )

    def _probe(self, iso_now):
        if not self._node:
            self.skipTest("node not found on PATH — skipping jsdom frontend tests")
        if not self._has_jsdom:
            self.skipTest(
                "jsdom not installed — run `cd tests/frontend && npm install` "
                "to enable frontend tests"
            )
        html = self.client.get(reverse("intro")).content.decode()
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
            fh.write(html)
            path = fh.name
        try:
            out = subprocess.run(
                [self._node, INTRO_PROBE, path, iso_now],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(
                out.returncode, 0, msg=f"probe failed: {out.stderr or out.stdout}"
            )
            return json.loads(out.stdout)
        finally:
            os.unlink(path)

    def test_new_tutor_starts_with_blank_calendar(self):
        # Davit (the default selected tutor) has no overrides and no custom times,
        # so the public calendar must show zero bookable slots — availability is
        # opt-in, never auto-generated.
        r = self._probe("2026-06-24T08:00:00")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertEqual(
            r["slotCount"], 0,
            "a tutor who set no availability must show no slots",
        )

    def test_explicitly_opened_slots_are_shown(self):
        # Once the tutor opens slots, exactly those appear (2 per opened day).
        self._open_davit((6, 24), (6, 25), (6, 26))
        r = self._probe("2026-06-24T08:00:00")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertEqual(r["slotCount"], 6)
        self.assertIn("24", r["dayNumbers"])

    def test_weekend_opens_on_first_week_with_slots(self):
        # Sun 28 Jun 2026: the current Mon–Fri week (22–26) is entirely past.
        # Davit is available the following week.
        self._open_davit((6, 29), (6, 30), (7, 1))
        r = self._probe("2026-06-28T12:00:00")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertGreater(
            r["slotCount"], 0,
            "weekend visitor must not land on a blank calendar",
        )
        # It advanced to the upcoming week (29 Jun–3 Jul), not the elapsed one.
        self.assertIn("29", r["dayNumbers"])
        self.assertNotIn("22", r["dayNumbers"])

    def test_midweek_stays_on_current_week(self):
        # Wed 24 Jun 2026 morning: the current week still has bookable days, so
        # the calendar must not skip ahead and hide today's remaining slots.
        self._open_davit((6, 24), (6, 25), (6, 26))
        r = self._probe("2026-06-24T08:00:00")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertGreater(r["slotCount"], 0)
        self.assertIn("24", r["dayNumbers"])
        
        
# Transactional e-mail for intro bookings (Resend via Anymail)
# --------------------------------------------------------------------------- #
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,  # send inline so mail.outbox is populated deterministically
    EMAIL_REPLY_TO="davit@thegreenpencil.at",
)

class IntroEmailTests(FluentDataMixin, TestCase):
    def _book(self, email="lena@example.at"):
        d = date.today() + timedelta(days=5)
        return self.client.post(
            "/api/intro-bookings/",
            data=json.dumps({
                "tutorSlug": "davit", "date": f"{d.year}-{d.month - 1}-{d.day}",
                "time": "14:00", "name": "Lena Gast", "email": email,
            }),
            content_type="application/json",
        )

    def test_guest_gets_confirmation_with_ics(self):
        from django.core import mail
        self.assertEqual(self._book().status_code, 200)
        guest = [m for m in mail.outbox if m.to == ["lena@example.at"]]
        self.assertEqual(len(guest), 1)
        msg = guest[0]
        self.assertIn("Schnupperstunde", msg.subject)
        self.assertEqual(msg.reply_to, ["davit@thegreenpencil.at"])
        # multipart: a plaintext body + an HTML alternative
        self.assertIn("Lena", msg.body)
        self.assertTrue(any(ct == "text/html" for _, ct in msg.alternatives))
        # calendar invite attached
        ics = [a for a in msg.attachments if a[0] == "schnupperstunde.ics"]
        self.assertEqual(len(ics), 1)
        self.assertIn("BEGIN:VEVENT", ics[0][1])
        self.assertIn("text/calendar", ics[0][2])

    @override_settings(TUTOR_NOTIFY_EMAIL="studio@thegreenpencil.at")
    def test_tutor_is_notified_when_configured(self):
        from django.core import mail
        self._book()
        tutor_mail = [m for m in mail.outbox if m.to == ["studio@thegreenpencil.at"]]
        self.assertEqual(len(tutor_mail), 1)
        self.assertIn("Lena Gast", tutor_mail[0].subject)
        self.assertIn("lena@example.at", tutor_mail[0].body)

    def test_no_tutor_mail_when_unconfigured(self):
        from django.core import mail
        self._book()  # TUTOR_NOTIFY_EMAIL unset by default
        self.assertEqual(len(mail.outbox), 1)  # guest only

    def test_email_failure_never_breaks_booking(self):
        # Even if the e-mail layer throws, the booking must still succeed.
        with mock.patch("core.emails.send_intro_confirmation", side_effect=RuntimeError("ESP down")):
            resp = self._book()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Booking.objects.filter(is_intro=True, guest_email="lena@example.at").exists())

    def test_ics_event_is_fifty_minutes(self):
        from core.emails import build_ics
        import re
        d = date.today() + timedelta(days=5)
        b = Booking.objects.create(
            tutor=self.davit, date=d, time="14:00", is_intro=True,
            guest_name="Lena Gast", guest_email="lena@example.at",
            student_name="Lena Gast", student_slug="intro",
        )
        ics = build_ics(b)
        start = re.search(r"DTSTART:(\d{8}T\d{6}Z)", ics).group(1)
        end = re.search(r"DTEND:(\d{8}T\d{6}Z)", ics).group(1)
        from datetime import datetime
        fmt = "%Y%m%dT%H%M%SZ"
        delta = datetime.strptime(end, fmt) - datetime.strptime(start, fmt)
        self.assertEqual(delta, timedelta(minutes=50))
