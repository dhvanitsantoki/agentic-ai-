from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.db.models import Q
import mimetypes

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .audit import audit
from .forms import CompanyProfileForm, SystemSettingsForm, UserCreateForm, UserEditForm
from .models import AuditLog, CompanyProfile, LoginAttempt, Notification, SystemSettings, has_active_two_factor
from .permissions import admin_required, is_manager

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class ThrottledLoginView(auth_views.LoginView):
    """Login view that locks a username/IP after repeated failures."""

    def _is_throttled(self, username, ip):
        since = timezone.now() - timezone.timedelta(minutes=LOCKOUT_MINUTES)
        failures = LoginAttempt.objects.filter(created_at__gte=since, success=False).filter(
            Q(username=username) | Q(ip_address=ip)
        )
        return failures.count() >= MAX_FAILED_LOGINS

    pending_two_factor = False

    def post(self, request, *args, **kwargs):
        username = (request.POST.get("username") or "")[:150]
        ip = _client_ip(request)
        if self._is_throttled(username, ip):
            return render(request, self.template_name, {"throttled": True, "form": self.get_form()}, status=429)
        response = super().post(request, *args, **kwargs)
        success = request.user.is_authenticated or self.pending_two_factor
        LoginAttempt.objects.create(username=username, ip_address=ip, success=success)
        if request.user.is_authenticated:
            audit("auth.login", user=request.user, summary=f"Login from {ip}")
        return response

    def form_valid(self, form):
        """Password is correct. Users with two-step login must still enter their code."""
        from .two_factor import start_pending_login
        from .models import has_active_two_factor

        user = form.get_user()
        if has_active_two_factor(user):
            self.pending_two_factor = True
            return start_pending_login(self.request, user, self.get_success_url())
        return super().form_valid(form)


login_view = login_not_required(ThrottledLoginView.as_view())


@login_not_required
def healthz(request):
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return JsonResponse({"status": "ok", "time": timezone.now().isoformat()})


def settings_home(request):
    sys = SystemSettings.get_solo()
    profile = CompanyProfile.get_solo()
    from apps.mailbox.models import MailAccount

    return render(request, "core/settings_home.html", {
        "sys": sys,
        "profile": profile,
        "profile_missing": profile.missing_for_outreach(),
        "mail_accounts": MailAccount.objects.all(),
        "ai_configured": bool(settings.ANTHROPIC_API_KEY),
        "openrouter_configured": bool(settings.OPENROUTER_API_KEY),
        "uses_openrouter": "openrouter" in (sys.ai_provider_drafting, sys.ai_provider_classification),
        "oauth": {
            "gmail": bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET),
            "outlook": bool(settings.MS_OAUTH_CLIENT_ID and settings.MS_OAUTH_CLIENT_SECRET),
            "zoho": bool(settings.ZOHO_OAUTH_CLIENT_ID and settings.ZOHO_OAUTH_CLIENT_SECRET),
        },
        "encryption_key_set": bool(settings.FIELD_ENCRYPTION_KEY),
        "my_two_factor": has_active_two_factor(request.user),
        "users_without_2fa": User.objects.filter(is_active=True).exclude(two_factor__confirmed_at__isnull=False).count(),
    })


@admin_required
def company_profile_edit(request):
    profile = CompanyProfile.get_solo()
    form = CompanyProfileForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by = request.user
        obj.save()
        audit("settings.company_profile", obj, user=request.user, summary="Company profile updated",
              changed=form.changed_data)
        messages.success(request, "Company profile saved.")
        return redirect("core:settings_home")
    return render(request, "core/form_page.html", {
        "form": form, "title": "Company profile",
        "subtitle": "Only verified information. The AI agents may use nothing about the company that is not written here.",
        "multipart": True,
    })


@admin_required
def system_settings_edit(request):
    sys = SystemSettings.get_solo()
    form = SystemSettingsForm(request.POST or None, instance=sys)
    if request.method == "POST" and form.is_valid():
        before_mode = SystemSettings.get_solo().execution_mode
        obj = form.save()
        audit("settings.system", obj, user=request.user, summary="System settings updated",
              changed=form.changed_data, mode_before=before_mode, mode_after=obj.execution_mode)
        messages.success(request, "Settings saved.")
        return redirect("core:settings_home")
    return render(request, "core/form_page.html", {
        "form": form, "title": "System settings",
        "subtitle": "Execution mode, sending policy, research limits and AI budget.",
    })


User = get_user_model()


@admin_required
def user_list(request):
    users = User.objects.select_related("profile", "two_factor").order_by("username")
    return render(request, "core/user_list.html", {"users": users})


@admin_required
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        audit("users.create", user, user=request.user, summary=f"User {user.username} created",
              role=form.cleaned_data["role"])
        messages.success(request, f"User {user.username} created.")
        return redirect("core:user_list")
    return render(request, "core/form_page.html", {"form": form, "title": "New user"})


@admin_required
def user_edit(request, pk):
    obj = get_object_or_404(User, pk=pk)
    form = UserEditForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        if obj == request.user and form.cleaned_data["role"] != "admin":
            messages.error(request, "You cannot remove your own admin role.")
        else:
            form.save()
            audit("users.update", obj, user=request.user, summary=f"User {obj.username} updated",
                  changed=form.changed_data)
            messages.success(request, "User updated.")
            return redirect("core:user_list")
    return render(request, "core/form_page.html", {"form": form, "title": f"Edit user {obj.username}"})


def notifications(request):
    q = Q(user=request.user)
    if is_manager(request.user):
        q |= Q(user__isnull=True)
    qs = Notification.objects.filter(q)
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "core/notifications.html", {"page_obj": page})


@require_POST
def notifications_mark_read(request):
    q = Q(user=request.user)
    if is_manager(request.user):
        q |= Q(user__isnull=True)
    Notification.objects.filter(q, is_read=False).update(is_read=True)
    return redirect("core:notifications")


@admin_required
def audit_log(request):
    qs = AuditLog.objects.select_related("user")
    action = request.GET.get("action", "").strip()
    if action:
        qs = qs.filter(action__icontains=action)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/audit_log.html", {"page_obj": page, "action": action})


INLINE_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp", "text/plain", "text/csv"}


def media(request, path):
    """Uploaded files, for signed-in users only (LoginRequiredMiddleware), from disk or database storage."""
    try:
        size = default_storage.size(path)
    except (FileNotFoundError, ValueError, SuspiciousFileOperation, OSError):
        raise Http404("File not found") from None
    if settings.SERVERLESS and size > settings.SERVERLESS_MAX_BODY_BYTES:
        return render(request, "core/file_too_large.html", {"name": path.rsplit("/", 1)[-1], "size": size}, status=413)
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    response = FileResponse(default_storage.open(path, "rb"), content_type=content_type,
                            as_attachment=content_type not in INLINE_TYPES, filename=path.rsplit("/", 1)[-1])
    response["Cache-Control"] = "private, max-age=3600"
    return response
