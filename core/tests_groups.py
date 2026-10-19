"""Families (siblings sharing one credit balance), student groups, group
credits and group sessions (one Booking per member)."""
import json
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from .models import (
    Booking, CreditTransaction, ErrorCard, Family, Receipt, SessionExercise, SessionFile,
    StudentGroup, User,
)
from .tests import FrozenTodayMixin, TestCase, extract_payload, make_user


class GroupsBase(FrozenTodayMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.davit = make_user("davit", "tutor", first_name="Davit", last_name="Petrosyan")
        self.admin = make_user("admin", "admin", first_name="Studio", last_name="Admin")
        self.anna = make_user("anna", "student", first_name="Anna", last_name="Huber", credits=4)
        self.ben = make_user("ben", "student", first_name="Ben", last_name="Huber", credits=1)
        self.cleo = make_user("cleo", "student", first_name="Cleo", last_name="Maier", group_credits=2)

    def post(self, user, url, body, method="post"):
        self.client.force_login(user)
        return getattr(self.client, method)(url, data=json.dumps(body), content_type="application/json")

    def make_family(self, *members):
        r = self.post(self.admin, "/api/families/", {"name": "Familie Huber", "members": [m.slug for m in members]})
        self.assertEqual(r.status_code, 200, r.content)
        return Family.objects.get(pk=r.json()["family"]["id"])

    def make_group(self, *members):
        r = self.post(self.admin, "/api/groups/", {
            "name": "Matura-Gruppe", "tutorSlug": "davit", "members": [m.slug for m in members]})
        self.assertEqual(r.status_code, 200, r.content)
        return StudentGroup.objects.get(pk=r.json()["group"]["id"])

    def book_group(self, group, d="2026-06-10", t="15:00"):
        return self.post(self.davit, "/api/group-sessions/", {
            "groupId": group.pk, "tutorSlug": "davit", "date": d, "time": t})


class FamilyTests(GroupsBase):
    def test_joining_pools_balances(self):
        fam = self.make_family(self.anna, self.ben)
        self.assertEqual(fam.credits, 5)
        self.anna.refresh_from_db()
        self.assertEqual((self.anna.credits, self.anna.balance()), (0, 5))
        self.assertEqual(self.ben.__class__.objects.get(pk=self.ben.pk).balance(), 5)

    def test_booking_one_sibling_charges_the_family(self):
        fam = self.make_family(self.anna, self.ben)
        r = self.post(self.davit, "/api/bookings/", {
            "studentSlug": "ben", "tutorSlug": "davit", "date": "2026-06-10", "time": "15:00"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["credits"], 4)
        fam.refresh_from_db()
        self.assertEqual(fam.credits, 4)
        # Cancelling refunds the shared balance.
        r = self.post(self.davit, f"/api/bookings/{r.json()['pk']}/", {}, method="delete")
        fam.refresh_from_db()
        self.assertEqual(fam.credits, 5)

    def test_student_self_booking_uses_family_balance(self):
        self.ben.credits = 0
        self.ben.save()
        self.anna.credits = 2
        self.anna.save()
        self.make_family(self.anna, self.ben)
        r = self.post(self.ben, "/api/bookings/", {
            "studentSlug": "ben", "tutorSlug": "davit", "date": "2026-06-10", "time": "15:00"})
        self.assertEqual(r.status_code, 200, r.content)

    def test_purchase_lands_in_family_and_receipt_names_buyer(self):
        fam = self.make_family(self.anna, self.ben)
        r = self.post(self.davit, "/api/credits/ben/", {"n": 10})
        self.assertEqual(r.status_code, 200, r.content)
        fam.refresh_from_db()
        self.assertEqual(fam.credits, 15)
        self.assertEqual(Receipt.objects.get().student, self.ben)

    def test_student_payload_shows_shared_balance_and_sibling_ledger(self):
        self.make_family(self.anna, self.ben)
        self.post(self.davit, "/api/bookings/", {
            "studentSlug": "ben", "tutorSlug": "davit", "date": "2026-06-10", "time": "15:00"})
        self.client.force_login(self.anna)
        data = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertEqual(data["currentUser"]["credits"], 4)
        self.assertEqual(data["currentUser"]["familyName"], "Familie Huber")
        self.assertTrue(any(t["studentId"] == "ben" for t in data["transactions"]["anna"]))
        self.assertEqual(data["families"], [])  # the family list is admin-only

    def test_leaving_keeps_balance_with_family_last_takes_rest(self):
        fam = self.make_family(self.anna, self.ben)
        r = self.post(self.admin, f"/api/families/{fam.pk}/", {"members": ["anna"]}, method="put")
        self.assertEqual(r.status_code, 200)
        self.ben.refresh_from_db()
        self.assertEqual((self.ben.family_id, self.ben.balance()), (None, 0))
        self.post(self.admin, f"/api/families/{fam.pk}/", {}, method="delete")
        self.anna.refresh_from_db()
        self.assertEqual((self.anna.family_id, self.anna.credits), (None, 5))
        self.assertFalse(Family.objects.exists())

    def test_cancelling_a_purchase_reverses_from_family(self):
        fam = self.make_family(self.anna, self.ben)
        self.post(self.davit, "/api/credits/anna/", {"n": 5})
        txn = CreditTransaction.objects.get(txn_type="buy")
        r = self.post(self.admin, f"/api/transactions/{txn.pk}/cancel/", {})
        self.assertEqual(r.status_code, 200, r.content)
        fam.refresh_from_db()
        self.assertEqual(fam.credits, 5)
        self.assertEqual(r.json()["credits"], 5)

    def test_only_admin_manages_families(self):
        r = self.post(self.davit, "/api/families/", {"name": "X", "members": []})
        self.assertEqual(r.status_code, 403)


class GroupSessionTests(GroupsBase):
    def test_group_session_books_a_seat_per_member_with_group_credits(self):
        g = self.make_group(self.anna, self.cleo)
        r = self.book_group(g)
        self.assertEqual(r.status_code, 200, r.content)
        seats = Booking.objects.filter(group_session=r.json()["groupSession"])
        self.assertEqual(seats.count(), 2)
        self.assertEqual({(s.credit_kind, s.group_name) for s in seats}, {("group", "Matura-Gruppe")})
        self.anna.refresh_from_db()
        self.cleo.refresh_from_db()
        self.assertEqual((self.anna.credits, self.anna.group_credits), (4, -1))
        self.assertEqual(self.cleo.group_credits, 1)

    def test_group_credits_floor_blocks_whole_session(self):
        self.anna.group_credits = -10
        self.anna.save()
        g = self.make_group(self.anna, self.cleo)
        r = self.book_group(g)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["students"], ["Anna Huber"])
        self.assertFalse(Booking.objects.exists())

    def test_slot_taken_by_single_lesson(self):
        Booking.objects.create(student=self.ben, tutor=self.davit, date=date(2026, 6, 10), time="15:00")
        r = self.book_group(self.make_group(self.anna, self.cleo))
        self.assertEqual(r.status_code, 409)

    def test_cancel_one_seat_refunds_group_credit(self):
        g = self.make_group(self.anna, self.cleo)
        key = self.book_group(g).json()["groupSession"]
        seat = Booking.objects.get(group_session=key, student=self.cleo)
        r = self.post(self.cleo, f"/api/bookings/{seat.pk}/", {}, method="delete")
        self.assertEqual(r.status_code, 200)
        self.cleo.refresh_from_db()
        self.assertEqual(self.cleo.group_credits, 2)
        self.assertEqual(Booking.objects.filter(group_session=key).count(), 1)

    def test_tutor_cancels_whole_group(self):
        g = self.make_group(self.anna, self.cleo)
        key = self.book_group(g).json()["groupSession"]
        seat = Booking.objects.filter(group_session=key).first()
        self.client.force_login(self.davit)
        r = self.client.delete(f"/api/bookings/{seat.pk}/?group=all")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Booking.objects.filter(group_session=key).exists())
        self.cleo.refresh_from_db()
        self.assertEqual(self.cleo.group_credits, 2)

    def test_tutor_edits_reach_every_seat_but_homework_done_is_own(self):
        g = self.make_group(self.anna, self.cleo)
        key = self.book_group(g).json()["groupSession"]
        a = Booking.objects.get(group_session=key, student=self.anna)
        c = Booking.objects.get(group_session=key, student=self.cleo)
        self.post(self.davit, f"/api/bookings/{a.pk}/", {"summary": "Past tense", "homework": "Ex. 3",
                                                       "time": "16:00"}, method="put")
        c.refresh_from_db()
        self.assertEqual((c.summary, c.homework, c.time), ("Past tense", "Ex. 3", "16:00"))
        self.post(self.cleo, f"/api/bookings/{c.pk}/", {"homeworkDone": True}, method="put")
        a.refresh_from_db()
        self.assertFalse(a.homework_done)
        # A student can't move a group seat on their own.
        r = self.post(self.cleo, f"/api/bookings/{c.pk}/", {"time": "17:00"}, method="put")
        self.assertEqual(r.status_code, 400)

    def test_error_cards_copied_to_every_student(self):
        g = self.make_group(self.anna, self.cleo)
        key = self.book_group(g).json()["groupSession"]
        a = Booking.objects.get(group_session=key, student=self.anna)
        r = self.post(self.davit, "/api/error-cards/", {
            "studentSlug": "anna", "bookingPk": a.pk, "front": "he go", "back": "he goes"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.json()["cards"]), 2)
        self.assertEqual(set(ErrorCard.objects.values_list("student__slug", flat=True)), {"anna", "cleo"})
        r = self.post(self.davit, "/api/error-cards/", {
            "studentSlug": "anna", "bookingPk": a.pk, "kind": "vocab",
            "items": [{"front": "w1", "back": "m1"}, {"front": "w2", "back": "m2"}]})
        self.assertEqual(len(r.json()["cards"]), 4)

    def test_exercises_fan_out_and_score_per_student(self):
        g = self.make_group(self.anna, self.cleo)
        key = self.book_group(g).json()["groupSession"]
        a = Booking.objects.get(group_session=key, student=self.anna)
        r = self.post(self.davit, f"/api/bookings/{a.pk}/exercises/", {"title": "Gap fill"})
        self.assertEqual(SessionExercise.objects.filter(title="Gap fill").count(), 2)
        ex = r.json()["exercise"]["id"]
        self.post(self.davit, f"/api/exercises/{ex}/", {"title": "Gap fill 2", "right": 3, "total": 5,
                                                         "level": "B1.1", "skill": "grammar"}, method="put")
        twin = SessionExercise.objects.exclude(pk=ex).get()
        self.assertEqual((twin.title, twin.score_right), ("Gap fill 2", None))

    def test_worksheets_shared_assignments_individual(self):
        g = self.make_group(self.anna, self.cleo)
        key = self.book_group(g).json()["groupSession"]
        a = Booking.objects.get(group_session=key, student=self.anna)
        c = Booking.objects.get(group_session=key, student=self.cleo)
        self.client.force_login(self.davit)
        self.client.post(f"/api/bookings/{a.pk}/files/", {"file": SimpleUploadedFile("sheet.pdf", b"%PDF-1")})
        self.client.post(f"/api/bookings/{a.pk}/files/",
                         {"file": SimpleUploadedFile("hw.pdf", b"%PDF-2"), "kind": "assignment"})
        self.assertEqual(SessionFile.objects.filter(kind="worksheet").count(), 1)
        self.assertEqual(SessionFile.objects.filter(kind="assignment", booking=c).count(), 1)
        sheet = SessionFile.objects.get(kind="worksheet")
        # Cleo sees the shared worksheet on her seat and may draw on it ...
        self.client.force_login(self.cleo)
        data = extract_payload(self.client.get(reverse("app")).content.decode())
        mine = next(b for b in data["bookings"] if b["pk"] == c.pk)
        self.assertEqual({f["kind"] for f in mine["files"]}, {"worksheet", "assignment"})
        self.assertEqual(self.client.get(f"/api/session-files/{sheet.pk}/").status_code, 200)
        r = self.client.post(f"/api/session-files/{sheet.pk}/annotations/", data=json.dumps({"add": [
            {"id": "a1", "type": "pen", "page": 1, "color": "#000000", "w": 0.01, "pts": [[0.1, 0.1]]}]}),
            content_type="application/json")
        self.assertEqual(r.status_code, 200)
        # ... but not Anna's own copy of the Hausübung.
        anna_hw = SessionFile.objects.get(kind="assignment", booking=a)
        self.assertEqual(self.client.get(f"/api/session-files/{anna_hw.pk}/").status_code, 404)
        # An outsider sees nothing.
        self.client.force_login(self.ben)
        self.assertEqual(self.client.get(f"/api/session-files/{sheet.pk}/").status_code, 404)


class GroupCreditPurchaseTests(GroupsBase):
    def test_tutor_grants_group_credits_with_group_receipt(self):
        r = self.post(self.davit, "/api/credits/cleo/", {"n": 5, "kind": "group"})
        self.assertEqual(r.status_code, 200, r.content)
        self.cleo.refresh_from_db()
        self.assertEqual((self.cleo.credits, self.cleo.group_credits), (0, 7))
        receipt = Receipt.objects.get()
        self.assertEqual((receipt.credit_kind, receipt.unit_price_cents), ("group", 30))
        self.assertEqual(CreditTransaction.objects.get(txn_type="buy").credit_kind, "group")

    def test_stripe_session_with_group_kind_grants_group_credits(self):
        from .views import credit_from_stripe_session
        receipt = credit_from_stripe_session({
            "id": "cs_1", "payment_status": "paid",
            "metadata": {"student_slug": "cleo", "credits": "3", "credit_kind": "group"}})
        self.assertEqual(receipt.credit_kind, "group")
        self.cleo.refresh_from_db()
        self.assertEqual(self.cleo.group_credits, 5)

    def test_payload_carries_group_price_and_groups(self):
        self.make_group(self.anna, self.cleo)
        self.client.force_login(self.ben)
        data = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertEqual(data["settings"]["groupCreditPrice"], 30)
        self.assertEqual(data["groups"], [])  # not his group
        self.client.force_login(self.cleo)
        data = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertEqual(data["groups"][0]["name"], "Matura-Gruppe")
        self.assertEqual(data["currentUser"]["groupCredits"], 2)
