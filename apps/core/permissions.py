"""Role-based access helpers."""
from functools import wraps

from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied

from .models import Role


def role_of(user) -> str:
    if not user or not user.is_authenticated:
        return ""
    if user.is_superuser:
        return Role.ADMIN
    profile = getattr(user, "profile", None)
    return profile.role if profile else Role.VIEWER


def has_role(user, *roles) -> bool:
    return role_of(user) in roles


def is_admin(user) -> bool:
    return has_role(user, Role.ADMIN)


def is_manager(user) -> bool:
    """Managers and admins may approve sensitive/commercial items."""
    return has_role(user, Role.ADMIN, Role.MANAGER)


def can_edit(user) -> bool:
    return has_role(user, Role.ADMIN, Role.MANAGER, Role.EXECUTIVE)


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not has_role(request.user, *roles):
                raise PermissionDenied("Your role does not allow this action.")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


manager_required = role_required(Role.ADMIN, Role.MANAGER)
admin_required = role_required(Role.ADMIN)
editor_required = role_required(Role.ADMIN, Role.MANAGER, Role.EXECUTIVE)


class RoleRequiredMixin(AccessMixin):
    allowed_roles: tuple = (Role.ADMIN, Role.MANAGER, Role.EXECUTIVE)

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not has_role(request.user, *self.allowed_roles):
            raise PermissionDenied("Your role does not allow this action.")
        return super().dispatch(request, *args, **kwargs)


class ManagerRequiredMixin(RoleRequiredMixin):
    allowed_roles = (Role.ADMIN, Role.MANAGER)


class AdminRequiredMixin(RoleRequiredMixin):
    allowed_roles = (Role.ADMIN,)


def safe_next(request, default: str) -> str:
    """Return the POSTed/GET 'next' URL only if it points to this site (prevents open redirects)."""
    from django.urls import reverse
    from django.utils.http import url_has_allowed_host_and_scheme

    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return default if default.startswith("/") else reverse(default)
