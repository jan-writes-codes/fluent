from django.db import models
from django.contrib.auth.models import AbstractUser
import json


class User(AbstractUser):
    ROLES = [('student', 'Student'), ('tutor', 'Tutor'), ('admin', 'Admin')]
    role = models.CharField(max_length=10, choices=ROLES, default='student')
    slug = models.CharField(max_length=50, unique=True, default='')
    initials = models.CharField(max_length=4, default='')
    credits = models.IntegerField(default=0)
    photo = models.TextField(blank=True, null=True)  # base64 data URL
    color1 = models.CharField(max_length=20, default='#c2714d')
    color2 = models.CharField(max_length=20, default='#a85535')
    billing_name = models.CharField(max_length=200, blank=True)
    billing_line1 = models.CharField(max_length=200, blank=True)
    billing_postcode = models.CharField(max_length=20, blank=True)
    billing_city = models.CharField(max_length=100, blank=True)
    billing_country = models.CharField(max_length=100, default='Österreich')
    receipt_seq = models.IntegerField(default=1000)

    class Meta:
        db_table = 'core_user'

    def __str__(self):
        return f'{self.slug} ({self.role})'


class Booking(models.Model):
    # SET_NULL (not CASCADE): a held/completed lesson is part of the immutable
    # history and must survive the deletion of a student or tutor account. The
    # *_slug / *_name snapshots below keep the row self-describing once the FK is
    # detached (GDPR erasure removes the account, the record itself is untouched).
    student = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='student_bookings'
    )
    tutor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='tutor_bookings'
    )
    student_slug = models.CharField(max_length=50, blank=True)
    student_name = models.CharField(max_length=200, blank=True)
    tutor_slug = models.CharField(max_length=50, blank=True)
    tutor_name = models.CharField(max_length=200, blank=True)
    date = models.DateField()
    time = models.CharField(max_length=5)  # "HH:MM"
    title = models.CharField(max_length=200, default='English session')
    notes = models.TextField(blank=True)
    tutor_notes = models.TextField(blank=True)
    call_link = models.TextField(blank=True)
    # Free "intro" session booked by a visitor from the public landing page, who
    # has no account yet. The guest's contact details live here (not on a User),
    # an intro never consumes credits, and it's capped at one per e-mail.
    is_intro = models.BooleanField(default=False)
    guest_name = models.CharField(max_length=200, blank=True)
    guest_email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'time']

    def save(self, *args, **kwargs):
        # Keep the identity snapshot in sync with the live FK while the accounts
        # exist; once an account is deleted the FK goes NULL and the last-known
        # snapshot is what remains.
        if self.student_id and self.student:
            self.student_slug = self.student.slug
            self.student_name = self.student.get_full_name() or self.student.username
        if self.tutor_id and self.tutor:
            self.tutor_slug = self.tutor.slug
            self.tutor_name = self.tutor.get_full_name() or self.tutor.username
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.student_slug} + {self.tutor_slug} on {self.date} at {self.time}'


class CreditTransaction(models.Model):
    # SET_NULL keeps the financial ledger intact after the student is deleted;
    # the slug/name snapshot below preserves who the entry belonged to.
    student = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions'
    )
    student_slug = models.CharField(max_length=50, blank=True)
    student_name = models.CharField(max_length=200, blank=True)
    txn_type = models.CharField(max_length=10)  # book, buy, done
    label = models.CharField(max_length=200)
    sub = models.CharField(max_length=200, blank=True)
    amount = models.IntegerField(default=0)
    receipt_no = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.student_slug}: {self.label}'


class Receipt(models.Model):
    number = models.CharField(max_length=30, unique=True)
    # SET_NULL: an issued receipt is an accounting document with a statutory
    # retention period (§ 132 BAO, 7 years) — it must outlive the account it was
    # issued to. The billing snapshot below freezes the recipient's details at
    # issue time (also the correct invoicing behaviour: a receipt never changes
    # because the customer later edits their address).
    student = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    student_slug = models.CharField(max_length=50, blank=True)
    student_name = models.CharField(max_length=200, blank=True)
    billing_name = models.CharField(max_length=200, blank=True)
    billing_line1 = models.CharField(max_length=200, blank=True)
    billing_postcode = models.CharField(max_length=20, blank=True)
    billing_city = models.CharField(max_length=100, blank=True)
    billing_country = models.CharField(max_length=100, blank=True)
    date_str = models.CharField(max_length=20)
    credits = models.IntegerField()
    unit_price_cents = models.IntegerField()  # in cents to avoid float
    # Stripe Checkout session that paid for this receipt (empty for receipts the
    # tutor added manually). Used to make webhook/redirect crediting idempotent.
    stripe_session_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # At most one receipt per Stripe session (empty = manual receipts,
            # which are unconstrained). This is the database-level guard that
            # makes webhook + redirect crediting idempotent under a race.
            models.UniqueConstraint(
                fields=['stripe_session_id'],
                condition=~models.Q(stripe_session_id=''),
                name='unique_stripe_session',
            )
        ]

    def __str__(self):
        return self.number


class AvailabilityOverride(models.Model):
    tutor = models.ForeignKey(User, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.CharField(max_length=5)
    is_open = models.BooleanField(default=True)

    class Meta:
        unique_together = ['tutor', 'date', 'time']

    def __str__(self):
        return f'{self.tutor.slug} {self.date} {self.time} {"open" if self.is_open else "closed"}'


class CustomTime(models.Model):
    tutor = models.ForeignKey(User, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.CharField(max_length=5)

    class Meta:
        unique_together = ['tutor', 'date', 'time']

    def __str__(self):
        return f'{self.tutor.slug} {self.date} {self.time}'


class StudentNote(models.Model):
    tutor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notes_created')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notes_received')
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Note on {self.student.slug} by {self.tutor.slug}'


class ActiveLesson(models.Model):
    student = models.ForeignKey(User, on_delete=models.CASCADE)
    lesson_id = models.CharField(max_length=20)

    class Meta:
        unique_together = ['student', 'lesson_id']

    def __str__(self):
        return f'{self.student.slug}: {self.lesson_id}'


def lesson_upload_path(instance, filename):
    return f'lessons/{instance.lesson_id}/{filename}'


class LessonFile(models.Model):
    """A PDF the tutor attaches to a curriculum lesson (e.g. 'a1-1'). Files are
    shared across all students who have that lesson unlocked."""
    lesson_id = models.CharField(max_length=20, db_index=True)
    file = models.FileField(upload_to=lesson_upload_path)
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL, related_name='uploaded_lesson_files'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']

    def __str__(self):
        return f'{self.lesson_id}: {self.original_name}'


class SiteSettings(models.Model):
    credit_price = models.IntegerField(default=30)  # EUR
    packs_json = models.TextField(
        default='[{"n":1,"price":"€32","each":"€32 / session","feat":false},'
                '{"n":5,"price":"€145","each":"€29 / session","feat":true,"tag":"Popular"},'
                '{"n":10,"price":"€270","each":"€27 / session","feat":false}]'
    )

    def __str__(self):
        return f'SiteSettings (credit_price={self.credit_price})'
