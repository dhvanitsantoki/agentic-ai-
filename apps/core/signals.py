from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Role, UserProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(
            user=instance, defaults={"role": Role.ADMIN if instance.is_superuser else Role.EXECUTIVE}
        )
