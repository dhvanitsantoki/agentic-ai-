"""Audit-trail helper used by every module for human and agent actions."""
import logging

from .models import AuditLog, Notification

log = logging.getLogger(__name__)


def audit(action: str, obj=None, *, user=None, agent: str = "", summary: str = "", **details) -> AuditLog:
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    entry = AuditLog.objects.create(
        user=user,
        agent=agent,
        action=action,
        object_type=obj._meta.label if obj is not None else "",
        object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
        summary=summary[:500],
        details=details,
    )
    log.info("audit %s %s %s", action, entry.object_type, summary[:200])
    return entry


def notify(title: str, *, body: str = "", url: str = "", level: str = "info", user=None) -> Notification:
    return Notification.objects.create(user=user, title=title[:200], body=body, url=url, level=level)
