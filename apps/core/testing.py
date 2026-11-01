"""Shared test helpers (no fake business data is ever created outside tests)."""
from django.contrib.auth import get_user_model

from apps.core.models import Role, UserProfile


def make_user(username="owner", role=Role.ADMIN, password="Str0ng-Passw0rd!", **extra):
    User = get_user_model()
    user = User.objects.create_user(username=username, password=password, email=f"{username}@example.test", **extra)
    profile = getattr(user, "profile", None) or UserProfile.objects.get_or_create(user=user)[0]
    profile.role = role
    profile.save()
    return user


def seed_reference_data():
    from apps.geo.seeds import seed_countries
    from apps.products.seeds import seed_categories

    seed_countries()
    seed_categories()
