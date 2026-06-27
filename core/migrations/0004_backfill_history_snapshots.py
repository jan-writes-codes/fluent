from django.db import migrations


def full_name(u):
    name = f"{u.first_name} {u.last_name}".strip()
    return name or u.username


def backfill(apps, schema_editor):
    """Freeze the identity/billing snapshot onto every existing history record so
    the rows stay self-describing once their account is deleted (FK -> NULL)."""
    Booking = apps.get_model("core", "Booking")
    CreditTransaction = apps.get_model("core", "CreditTransaction")
    Receipt = apps.get_model("core", "Receipt")

    for b in Booking.objects.select_related("student", "tutor").all():
        changed = False
        if b.student and not b.student_slug:
            b.student_slug = b.student.slug
            b.student_name = full_name(b.student)
            changed = True
        if b.tutor and not b.tutor_slug:
            b.tutor_slug = b.tutor.slug
            b.tutor_name = full_name(b.tutor)
            changed = True
        if changed:
            b.save(update_fields=["student_slug", "student_name", "tutor_slug", "tutor_name"])

    for t in CreditTransaction.objects.select_related("student").all():
        if t.student and not t.student_slug:
            t.student_slug = t.student.slug
            t.student_name = full_name(t.student)
            t.save(update_fields=["student_slug", "student_name"])

    for r in Receipt.objects.select_related("student").all():
        if r.student and not r.student_slug:
            u = r.student
            r.student_slug = u.slug
            r.student_name = full_name(u)
            r.billing_name = u.billing_name
            r.billing_line1 = u.billing_line1
            r.billing_postcode = u.billing_postcode
            r.billing_city = u.billing_city
            r.billing_country = u.billing_country
            r.save(update_fields=[
                "student_slug", "student_name", "billing_name", "billing_line1",
                "billing_postcode", "billing_city", "billing_country",
            ])


def noop(apps, schema_editor):
    # Snapshots are harmless to leave in place if rolled back.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_booking_student_name_booking_student_slug_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
