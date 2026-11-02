"""Create the first admin account from environment variables, only when no user exists yet.

Used by hosted deployments where nobody can run `createsuperuser` interactively (e.g. Vercel's build):
    OWNER_USERNAME          (default "owner")
    OWNER_INITIAL_PASSWORD  (required; change it after the first sign-in)
    OWNER_EMAIL             (optional)
Safe to run on every deploy: does nothing once any user exists.
"""
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Role, UserProfile


class Command(BaseCommand):
    help = "Create the first admin user from OWNER_* environment variables if there are no users."

    def handle(self, *args, **opts):
        User = get_user_model()
        if User.objects.exists():
            self.stdout.write("Users already exist - nothing to do.")
            return
        password = os.environ.get("OWNER_INITIAL_PASSWORD", "")
        if not password:
            self.stdout.write("OWNER_INITIAL_PASSWORD is not set - no admin user created.")
            return
        username = os.environ.get("OWNER_USERNAME", "owner").strip() or "owner"
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("OWNER_INITIAL_PASSWORD is too weak: " + " ".join(exc.messages)) from exc
        user = User.objects.create_superuser(username=username, email=os.environ.get("OWNER_EMAIL", ""), password=password)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = Role.ADMIN
        profile.save()
        self.stdout.write(self.style.SUCCESS(f"Admin user '{username}' created - change the password after signing in."))
