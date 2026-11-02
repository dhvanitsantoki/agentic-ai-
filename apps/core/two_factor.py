"""Two-step login: verify step after the password, setup with QR code, recovery codes, admin reset."""
import time

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_not_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from . import totp
from .audit import audit
from .models import CompanyProfile, LoginAttempt, SystemSettings, TwoFactorDevice, has_active_two_factor
from .permissions import admin_required

PENDING_KEY = "two_factor_pending"
PENDING_MAX_AGE = 300  # seconds to enter the code after the password
MAX_CODE_FAILURES = 5
SETUP_SECRET_KEY = "two_factor_setup_secret"


class CodeForm(forms.Form):
    code = forms.CharField(max_length=20, widget=forms.TextInput(attrs={
        "class": "form-control form-control-lg text-center", "autocomplete": "one-time-code", "inputmode": "numeric",
        "autofocus": True, "placeholder": "123456",
    }))


def start_pending_login(request, user, next_url):
    request.session[PENDING_KEY] = {"uid": user.pk, "next": next_url, "ts": time.time()}
    return redirect("two_factor_verify")


def _ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    return forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")


def _code_failures(username, ip):
    since = timezone.now() - timezone.timedelta(minutes=15)
    return LoginAttempt.objects.filter(created_at__gte=since, success=False).filter(
        Q(username=f"2fa:{username}") | Q(ip_address=ip)).count()


@login_not_required
def verify(request):
    pending = request.session.get(PENDING_KEY)
    if not pending or time.time() - pending.get("ts", 0) > PENDING_MAX_AGE:
        request.session.pop(PENDING_KEY, None)
        if pending:
            messages.warning(request, "The sign-in code step expired. Please sign in again.")
        return redirect("login")
    user = get_user_model().objects.filter(pk=pending["uid"], is_active=True).first()
    if user is None or not has_active_two_factor(user):
        request.session.pop(PENDING_KEY, None)
        return redirect("login")
    ip = _ip(request)
    form = CodeForm(request.POST or None)
    error = ""
    if request.method == "POST":
        if _code_failures(user.get_username(), ip) >= MAX_CODE_FAILURES:
            request.session.pop(PENDING_KEY, None)
            return render(request, "registration/two_factor_verify.html", {"form": form, "locked": True}, status=429)
        if form.is_valid():
            method = user.two_factor.verify(form.cleaned_data["code"])
            if method:
                request.session.pop(PENDING_KEY, None)
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                LoginAttempt.objects.create(username=f"2fa:{user.get_username()}", ip_address=ip, success=True)
                audit("auth.login", user=user, summary=f"Login with two-step code ({method}) from {ip}")
                if method == "recovery":
                    messages.warning(request, f"You used a recovery code. {user.two_factor.recovery_codes_left} left - "
                                              "create new ones under Two-step login if you are running low.")
                nxt = pending.get("next") or settings.LOGIN_REDIRECT_URL
                if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
                    nxt = "/"
                return redirect(nxt if nxt.startswith("/") else "/")
        LoginAttempt.objects.create(username=f"2fa:{user.get_username()}", ip_address=ip, success=False)
        error = "That code is not valid. Use the current 6-digit code from your authenticator app, or a recovery code."
    return render(request, "registration/two_factor_verify.html", {"form": form, "error": error})


def setup(request):
    """Show status, or set up a new authenticator app (QR code + confirmation code)."""
    device = getattr(request.user, "two_factor", None)
    if device and device.is_active:
        return render(request, "core/two_factor_status.html", {"device": device, "form": CodeForm(),
                                                               "required": SystemSettings.get_solo().require_two_factor})
    secret = request.session.get(SETUP_SECRET_KEY) or totp.new_secret()
    request.session[SETUP_SECRET_KEY] = secret
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        step = totp.verify(secret, form.cleaned_data["code"])
        if step is not None:
            device = device or TwoFactorDevice(user=request.user)
            device.secret = secret
            device.confirmed_at = timezone.now()
            device.last_used_step = step
            device.save()
            codes = device.new_recovery_codes()
            request.session.pop(SETUP_SECRET_KEY, None)
            audit("auth.2fa_enabled", device, user=request.user, summary="Two-step login enabled")
            return render(request, "core/two_factor_codes.html", {"codes": codes, "first_time": True})
        form.add_error("code", "Code did not match. Check the phone's time is automatic and try the newest code.")
    brand = CompanyProfile.get_solo().brand_name or "Flotec"
    uri = totp.provisioning_uri(secret, request.user.get_username(), f"{brand} Sales AI")
    return render(request, "core/two_factor_setup.html", {
        "form": form, "secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        "qr": mark_safe(totp.qr_svg(uri)),  # SVG generated locally from our own data
        "required": SystemSettings.get_solo().require_two_factor,
    })


@require_POST
def manage(request):
    """Regenerate recovery codes or switch two-step login off (both need a current code)."""
    device = getattr(request.user, "two_factor", None)
    if not device or not device.is_active:
        return redirect("two_factor_setup")
    form = CodeForm(request.POST)
    if not form.is_valid() or device.verify(form.cleaned_data["code"]) != "totp":
        messages.error(request, "Enter a valid current code from your authenticator app.")
        return redirect("two_factor_setup")
    if request.POST.get("action") == "codes":
        codes = device.new_recovery_codes()
        audit("auth.2fa_codes", device, user=request.user, summary="Recovery codes regenerated")
        return render(request, "core/two_factor_codes.html", {"codes": codes})
    if request.POST.get("action") == "disable":
        device.delete()
        audit("auth.2fa_disabled", user=request.user, summary="Two-step login switched off by the user")
        messages.warning(request, "Two-step login switched off.")
    return redirect("two_factor_setup")


@admin_required
@require_POST
def admin_reset(request, pk):
    """Admin removes a user's authenticator (lost phone). The user sets it up again at next sign-in."""
    user = get_object_or_404(get_user_model(), pk=pk)
    TwoFactorDevice.objects.filter(user=user).delete()
    audit("auth.2fa_reset", user=request.user, summary=f"Two-step login reset for {user.get_username()}")
    messages.success(request, f"Two-step login reset for {user.get_username()}.")
    return redirect("core:user_edit", pk=pk)


EXEMPT_PREFIXES = ("/accounts/", "/static/", "/healthz", "/u/")


class TwoFactorRequiredMiddleware:
    """When the owner requires two-step login, signed-in users without it can only reach the setup page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (user is not None and user.is_authenticated and not request.path.startswith(EXEMPT_PREFIXES)
                and SystemSettings.objects.filter(pk=1, require_two_factor=True).exists()
                and not has_active_two_factor(user)):
            return redirect("two_factor_setup")
        return self.get_response(request)
