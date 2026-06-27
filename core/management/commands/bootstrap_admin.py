"""Idempotently create the first admin from environment variables.

Designed to run on every deploy (e.g. at the end of a PaaS build command) on
hosts where there is no shell to run ``createadmin`` interactively — Render's
free tier, for example. It reads credentials from the environment and:

* does **nothing** when ``ADMIN_EMAIL``/``ADMIN_PASSWORD`` are unset, so it is
  harmless in dev/test and on hosts that don't want a bootstrap admin;
* **skips silently** when an admin with that email already exists, so it is safe
  to run on every redeploy (idempotent);
* otherwise delegates to ``createadmin`` to create the account.

    # set in the host's env, then add to the build/release step:
    python manage.py bootstrap_admin

Environment variables:
    ADMIN_EMAIL     Login email for the admin (required to do anything).
    ADMIN_PASSWORD  Password (required to do anything).
    ADMIN_NAME      Full name, e.g. "Jan Heissenberger" (optional).
"""
import os

from django.core.management import call_command
from django.core.management.base import BaseCommand

from core.models import User


class Command(BaseCommand):
    help = "Create the bootstrap admin from ADMIN_EMAIL/ADMIN_PASSWORD env vars (idempotent)."

    def handle(self, *args, **opts):
        email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
        password = os.environ.get("ADMIN_PASSWORD") or ""
        name = (os.environ.get("ADMIN_NAME") or "").strip()

        if not email or not password:
            self.stdout.write(
                "bootstrap_admin: ADMIN_EMAIL/ADMIN_PASSWORD not set — skipping."
            )
            return

        if User.objects.filter(email__iexact=email).exists():
            self.stdout.write(
                f"bootstrap_admin: admin {email} already exists — skipping."
            )
            return

        # Reuse createadmin so the User is built exactly the same way (role,
        # slug, initials, superuser flags). It is non-interactive here because
        # both --email and --password are supplied.
        call_command("createadmin", email=email, password=password, name=name)
