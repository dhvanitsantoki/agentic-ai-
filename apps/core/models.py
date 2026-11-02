"""Core models: company profile, system settings, roles, audit log, notifications."""
from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    ADMIN = "admin", "Admin / Owner"
    MANAGER = "manager", "Sales manager"
    EXECUTIVE = "executive", "Sales executive"
    VIEWER = "viewer", "Viewer (read-only)"


class ExecutionMode(models.IntegerChoices):
    RESEARCH_ONLY = 1, "Mode 1 – Research only"
    DRAFT_AND_APPROVE = 2, "Mode 2 – Draft and approval"
    CONTROLLED_AUTOMATION = 3, "Mode 3 – Controlled automation"


class AutomationState(models.TextChoices):
    RUNNING = "running", "Running"
    PAUSED = "paused", "Paused"
    STOPPED = "stopped", "Stopped"


class SingletonModel(models.Model):
    """A table that always holds exactly one row (pk=1)."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover - singletons are never deleted
        return (0, {})

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.EXECUTIVE)
    phone = models.CharField(max_length=40, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    email_signature = models.TextField(
        blank=True, help_text="Plain-text signature appended to e-mails this user approves/sends."
    )

    def __str__(self):
        return f"{self.user.get_username()} ({self.get_role_display()})"


class CompanyProfile(SingletonModel):
    """Verified facts about Ajay Plastic / Flotec. The AI may only use what is written here."""

    legal_name = models.CharField(max_length=200, default="Ajay Plastic")
    brand_name = models.CharField(max_length=100, default="Flotec")
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, default="Ahmedabad")
    state = models.CharField(max_length=100, default="Gujarat")
    country = models.CharField(max_length=100, default="India")
    website = models.URLField(blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    whatsapp = models.CharField(max_length=50, blank=True)
    year_established = models.PositiveIntegerField(null=True, blank=True)
    gstin = models.CharField("GSTIN", max_length=30, blank=True)
    iec_code = models.CharField("IEC code", max_length=30, blank=True)
    logo = models.FileField(upload_to="company/", blank=True,
                            validators=[FileExtensionValidator(["png", "jpg", "jpeg"])], help_text="PNG or JPG, used on quotations.")

    about = models.TextField(
        blank=True,
        help_text="Short, approved description of the company used in e-mails and quotations.",
    )
    approved_facts = models.TextField(
        blank=True,
        help_text="One verified fact per line (e.g. capacities, years of experience, markets served). "
        "The AI may only use facts listed here.",
    )
    manufacturing_capabilities = models.TextField(
        blank=True, help_text="One approved capability per line (e.g. in-house moulding, private-label packing)."
    )
    certifications = models.TextField(
        blank=True,
        help_text="One certification per line — ONLY certifications the company actually holds. "
        "Leave empty if none should be mentioned.",
    )
    oem_services = models.TextField(
        blank=True, help_text="One approved OEM/private-label service per line (e.g. custom colours, own-brand printing)."
    )

    # Default commercial terms (used by the quotation agent only if filled in here).
    default_payment_terms = models.CharField(max_length=255, blank=True)
    default_incoterm = models.CharField(max_length=20, blank=True)
    default_port_of_loading = models.CharField(max_length=120, blank=True)
    default_lead_time = models.CharField(max_length=120, blank=True)
    default_packaging = models.CharField(max_length=255, blank=True)
    quotation_validity_days = models.PositiveIntegerField(null=True, blank=True)
    quotation_terms = models.TextField(blank=True, help_text="Standard terms & conditions printed on quotations.")

    sender_name = models.CharField(max_length=120, blank=True, help_text="Default signature name.")
    sender_title = models.CharField(max_length=120, blank=True)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        verbose_name = "company profile"

    def __str__(self):
        return f"{self.legal_name} ({self.brand_name})"

    @staticmethod
    def _lines(text: str) -> list[str]:
        return [line.strip(" -•\t") for line in (text or "").splitlines() if line.strip(" -•\t")]

    @property
    def approved_fact_list(self):
        return self._lines(self.approved_facts)

    @property
    def capability_list(self):
        return self._lines(self.manufacturing_capabilities)

    @property
    def certification_list(self):
        return self._lines(self.certifications)

    @property
    def oem_service_list(self):
        return self._lines(self.oem_services)

    def missing_for_outreach(self) -> list[str]:
        """Profile fields that should be completed before the AI writes outreach e-mails."""
        missing = []
        if not self.about.strip():
            missing.append("About the company")
        if not (self.sender_name or "").strip():
            missing.append("Signature name")
        if not self.approved_fact_list and not self.capability_list:
            missing.append("Approved facts or manufacturing capabilities")
        return missing

    def facts_for_ai(self) -> dict:
        return {
            "company": self.legal_name,
            "brand": self.brand_name,
            "location": ", ".join(p for p in [self.city, self.state, self.country] if p),
            "website": self.website,
            "about": self.about,
            "year_established": self.year_established,
            "approved_facts": self.approved_fact_list,
            "manufacturing_capabilities": self.capability_list,
            "certifications_held": self.certification_list,
            "oem_services": self.oem_service_list,
        }


DEFAULT_MODEL = "claude-opus-5-5"
MODEL_CHOICES = [
    ("claude-opus-5-5", "Claude Opus 5.5 (highest quality, $4/$20 per M tokens)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 (balanced, $2/$10 per M tokens)"),
    ("claude-haiku-5-5", "Claude Haiku 5.5 (lowest cost, $0.10/$0.50 per M tokens)"),
]
EFFORT_CHOICES = [("low", "Low"), ("medium", "Medium"), ("high", "High")]
PROVIDER_CHOICES = [
    ("anthropic", "Anthropic Claude (uses the Claude model chosen above)"),
    ("openrouter", "OpenRouter (uses the OpenRouter models below)"),
]
DEFAULT_OPENROUTER_MODELS = "openrouter/free"


def default_auto_send_kinds():
    return ["initial", "followup"]


class SystemSettings(SingletonModel):
    execution_mode = models.IntegerField(choices=ExecutionMode.choices, default=ExecutionMode.DRAFT_AND_APPROVE)
    automation_state = models.CharField(
        max_length=10, choices=AutomationState.choices, default=AutomationState.PAUSED,
        help_text="Paused on a fresh installation until the owner has completed setup.",
    )
    automation_state_changed_at = models.DateTimeField(null=True, blank=True)
    automation_state_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # Sending policy
    auto_send_kinds = models.JSONField(
        default=default_auto_send_kinds,
        blank=True,
        help_text="E-mail kinds that may be sent without a human in Mode 3 (initial, followup, catalogue).",
    )
    daily_send_limit = models.PositiveIntegerField(default=40, help_text="Max e-mails per mailbox per day.")
    hourly_send_limit = models.PositiveIntegerField(default=10, help_text="Max e-mails per mailbox per hour.")
    send_window_start_hour = models.PositiveSmallIntegerField(default=9, help_text="Recipient local time, 0–23.")
    send_window_end_hour = models.PositiveSmallIntegerField(default=17, help_text="Recipient local time, 1–24.")
    skip_weekends = models.BooleanField(default=True, help_text="Do not send on Saturday/Sunday recipient local time.")
    min_days_between_initial_emails = models.PositiveIntegerField(
        default=180, help_text="Never send a second introduction to the same company within this many days."
    )
    unsubscribe_footer = models.TextField(
        default=(
            "If you would prefer not to receive further e-mails from us, "
            "you can unsubscribe here: {unsubscribe_url}"
        )
    )

    # Research limits
    research_daily_company_limit = models.PositiveIntegerField(default=30)
    research_companies_per_run = models.PositiveIntegerField(default=8)
    research_max_web_searches = models.PositiveIntegerField(default=12, help_text="Per research run.")

    # AI configuration
    ai_model_research = models.CharField(max_length=60, choices=MODEL_CHOICES, default=DEFAULT_MODEL)
    ai_model_drafting = models.CharField(max_length=60, choices=MODEL_CHOICES, default=DEFAULT_MODEL)
    ai_model_classification = models.CharField(max_length=60, choices=MODEL_CHOICES, default=DEFAULT_MODEL)
    ai_effort_research = models.CharField(max_length=10, choices=EFFORT_CHOICES, default="medium")
    ai_effort_drafting = models.CharField(max_length=10, choices=EFFORT_CHOICES, default="low")
    ai_effort_classification = models.CharField(max_length=10, choices=EFFORT_CHOICES, default="low")
    ai_provider_drafting = models.CharField(
        "AI provider for e-mail drafting", max_length=20, choices=PROVIDER_CHOICES, default="anthropic",
    )
    ai_provider_classification = models.CharField(
        "AI provider for reply classification and report summaries", max_length=20, choices=PROVIDER_CHOICES,
        default="anthropic",
        help_text="Buyer, OEM and country research always use Anthropic (they need its web search and fetch tools).",
    )
    openrouter_models = models.CharField(
        "OpenRouter models", max_length=500, default=DEFAULT_OPENROUTER_MODELS,
        help_text="Comma-separated OpenRouter model IDs; the first is preferred, the others are fallbacks. "
                  "'openrouter/free' picks a free model that supports structured output. Free models are rate-limited "
                  "and their providers may log or train on what is sent (including buyer e-mails) - check "
                  "openrouter.ai/settings/privacy.",
    )
    monthly_ai_budget_usd = models.DecimalField(
        max_digits=10, decimal_places=2, default=100,
        help_text="All AI calls stop when the metered spend this calendar month reaches this amount.",
    )

    # Security
    require_two_factor = models.BooleanField(
        default=False, help_text="Every user must set up two-step login (authenticator app) before using the system.",
    )

    # Notifications
    escalation_emails = models.TextField(blank=True, help_text="Comma-separated addresses notified of hot leads.")
    report_recipients = models.TextField(blank=True, help_text="Comma-separated addresses that receive the daily report.")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "system settings"
        verbose_name_plural = "system settings"

    def __str__(self):
        return "System settings"

    @property
    def is_running(self):
        return self.automation_state == AutomationState.RUNNING

    def set_automation_state(self, state: str, user=None):
        self.automation_state = state
        self.automation_state_changed_at = timezone.now()
        self.automation_state_changed_by = user
        self.save(update_fields=["automation_state", "automation_state_changed_at", "automation_state_changed_by", "updated_at"])

    @staticmethod
    def _split(text):
        return [e.strip() for e in (text or "").replace(";", ",").split(",") if e.strip()]

    @property
    def escalation_email_list(self):
        return self._split(self.escalation_emails)

    @property
    def report_recipient_list(self):
        return self._split(self.report_recipients)


class AuditLog(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    agent = models.CharField(max_length=40, blank=True, help_text="Agent name when the action was automated.")
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=40, blank=True)
    summary = models.CharField(max_length=500, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        who = self.user or self.agent or "system"
        return f"{self.created_at:%Y-%m-%d %H:%M} {who}: {self.action}"


class Notification(models.Model):
    LEVELS = [("info", "Info"), ("success", "Success"), ("warning", "Warning"), ("danger", "Urgent")]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
        help_text="Empty = visible to all managers and admins.",
    )
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=300, blank=True)
    level = models.CharField(max_length=10, choices=LEVELS, default="info")
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class LoginAttempt(models.Model):
    username = models.CharField(max_length=150, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    success = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class TwoFactorDevice(models.Model):
    """Authenticator-app (TOTP) device for two-step login. Secret encrypted, recovery codes hashed."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="two_factor")
    _secret = models.TextField(db_column="secret")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_step = models.BigIntegerField(null=True, blank=True)
    recovery_code_hashes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"2FA for {self.user}"

    @property
    def secret(self) -> str:
        from .crypto import decrypt

        return decrypt(self._secret)

    @secret.setter
    def secret(self, value: str):
        from .crypto import encrypt

        self._secret = encrypt(value)

    @property
    def is_active(self) -> bool:
        return self.confirmed_at is not None

    @staticmethod
    def _hash_code(code: str) -> str:
        import hashlib

        normal = "".join(ch for ch in (code or "").lower() if ch.isalnum())
        return hashlib.sha256(normal.encode()).hexdigest()

    def new_recovery_codes(self, count: int = 10) -> list[str]:
        import secrets as _secrets

        codes = [f"{_secrets.token_hex(3)}-{_secrets.token_hex(3)}" for _ in range(count)]
        self.recovery_code_hashes = [self._hash_code(c) for c in codes]
        self.save(update_fields=["recovery_code_hashes"])
        return codes

    @property
    def recovery_codes_left(self) -> int:
        return len(self.recovery_code_hashes or [])

    def verify(self, code: str) -> str:
        """Return 'totp', 'recovery' or '' (invalid). Each TOTP code and each recovery code works only once."""
        from . import totp

        step = totp.verify(self.secret, code, last_step=self.last_used_step)
        if step is not None:
            self.last_used_step = step
            self.save(update_fields=["last_used_step"])
            return "totp"
        hashed = self._hash_code(code)
        if hashed in (self.recovery_code_hashes or []):
            self.recovery_code_hashes = [h for h in self.recovery_code_hashes if h != hashed]
            self.save(update_fields=["recovery_code_hashes"])
            return "recovery"
        return ""


def has_active_two_factor(user) -> bool:
    device = getattr(user, "two_factor", None) if user and user.is_authenticated else None
    return bool(device and device.is_active)
