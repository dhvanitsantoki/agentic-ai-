"""Build step for Vercel (pyproject.toml [tool.vercel.scripts] build): prepares the database.

Runs migrations, the idempotent initial data and the first admin account (OWNER_* variables).
Without DATABASE_URL (database not connected yet) it only reports what is missing, so the first
deployment does not fail before the owner has added the database in the Vercel dashboard.
"""
import os

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Vercel build step: migrate, initial data, first admin user."

    def handle(self, *args, **opts):
        missing = [v for v in ("DJANGO_SECRET_KEY", "FIELD_ENCRYPTION_KEY", "CRON_SECRET") if not os.environ.get(v)]
        if missing:
            self.stdout.write(self.style.WARNING("Environment variables not set: " + ", ".join(missing)))
        if not os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://")):
            self.stdout.write(self.style.WARNING(
                "DATABASE_URL is not set: connect a PostgreSQL database (project -> Storage -> Neon) and redeploy. "
                "Skipping migrations."))
            return
        call_command("migrate", interactive=False, verbosity=1)
        call_command("setup_initial_data")
        call_command("ensure_owner")
        self.stdout.write(self.style.SUCCESS(f"Database ready ({settings.DATABASES['default']['HOST']})."))
