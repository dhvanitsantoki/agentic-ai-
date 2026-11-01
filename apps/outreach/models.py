"""Outbound e-mail (drafts, approvals, sent), templates and the suppression list."""
from django.conf import settings
from django.db import models
from django.utils import timezone


class TemplateKind(models.TextChoices):
    OEM_PROPOSAL = "oem_proposal", "OEM manufacturing proposal"
    PRIVATE_LABEL = "private_label", "Private-label manufacturing"
    DISTRIBUTOR = "distributor", "Distributor appointment"
    IMPORTER = "importer", "Importer introduction"
    FIRST_EXPORT = "first_export", "First export introduction"
    PRODUCT_SPECIFIC = "product_specific", "Product-specific introduction"
    EXHIBITION = "exhibition", "Exhibition follow-up"
    EXISTING_CUSTOMER = "existing_customer", "Existing customer follow-up"
    QUOTATION_FOLLOWUP = "quotation_followup", "Quotation follow-up"
    NEGOTIATION = "negotiation", "Negotiation and price discussion"


SENSITIVE_TEMPLATE_KINDS = {TemplateKind.QUOTATION_FOLLOWUP, TemplateKind.NEGOTIATION}


class EmailTemplate(models.Model):
    kind = models.CharField(max_length=20, choices=TemplateKind.choices, unique=True)
    name = models.CharField(max_length=120)
    purpose = models.TextField(help_text="What this e-mail must achieve.")
    guidance = models.TextField(help_text="Structure and rules the AI must follow when writing it.")
    example_subject = models.CharField(max_length=200, blank=True)
    call_to_action = models.CharField(max_length=300, blank=True)
    max_words = models.PositiveSmallIntegerField(default=170)
    approved_for_auto_send = models.BooleanField(
        default=False, help_text="In Mode 3, e-mails from this template may be sent without a human (if no sensitive content)."
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind"]

    def __str__(self):
        return self.name


class EmailKind(models.TextChoices):
    INITIAL = "initial", "Initial introduction"
    FOLLOWUP = "followup", "Follow-up"
    REPLY = "reply", "Reply to buyer"
    CATALOGUE = "catalogue", "Catalogue / information"
    QUOTATION = "quotation", "Quotation"
    MANUAL = "manual", "Manual e-mail"


class OutboundEmail(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Waiting for approval"
        APPROVED = "approved", "Approved – queued to send"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        BLOCKED = "blocked", "Blocked by policy"

    company = models.ForeignKey("crm.Company", on_delete=models.CASCADE, related_name="outbound_emails")
    contact = models.ForeignKey("crm.Contact", null=True, blank=True, on_delete=models.SET_NULL, related_name="outbound_emails")
    to_email = models.EmailField()
    cc_emails = models.CharField(max_length=500, blank=True)
    account = models.ForeignKey("mailbox.MailAccount", null=True, blank=True, on_delete=models.SET_NULL, related_name="outbound")
    kind = models.CharField(max_length=10, choices=EmailKind.choices)
    template = models.ForeignKey(EmailTemplate, null=True, blank=True, on_delete=models.SET_NULL)
    subject = models.CharField(max_length=300)
    body_text = models.TextField()
    attachments = models.ManyToManyField("products.CompanyDocument", blank=True, related_name="+")
    quotation = models.ForeignKey("quotations.Quotation", null=True, blank=True, on_delete=models.SET_NULL, related_name="emails")
    in_reply_to = models.ForeignKey("mailbox.InboundEmail", null=True, blank=True, on_delete=models.SET_NULL, related_name="draft_replies")
    enrollment = models.ForeignKey("followups.Enrollment", null=True, blank=True, on_delete=models.SET_NULL, related_name="emails")
    followup_step = models.PositiveSmallIntegerField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    requires_approval = models.BooleanField(default=True)
    approval_reasons = models.JSONField(default=list, blank=True)
    sensitive_flags = models.JSONField(default=list, blank=True)
    personalization = models.JSONField(default=dict, blank=True, help_text="Facts used and their sources.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by_agent = models.CharField(max_length=40, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.CharField(max_length=500, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True, help_text="Earliest send time (recipient local window).")
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    send_attempts = models.PositiveSmallIntegerField(default=0)
    provider_message_id = models.CharField(max_length=255, blank=True)
    provider_thread_id = models.CharField(max_length=255, blank=True, db_index=True)
    rfc_message_id = models.CharField(max_length=500, blank=True, db_index=True)
    error = models.TextField(blank=True)
    bounced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["enrollment", "followup_step"], name="uniq_followup_step_email",
                condition=models.Q(enrollment__isnull=False) & ~models.Q(status__in=["rejected", "cancelled", "blocked", "failed"]),
            )
        ]

    def __str__(self):
        return f"{self.get_kind_display()} to {self.to_email}: {self.subject}"

    @property
    def is_open(self):
        return self.status in (self.Status.DRAFT, self.Status.PENDING_APPROVAL, self.Status.APPROVED)


class SuppressionEntry(models.Model):
    class Reason(models.TextChoices):
        UNSUBSCRIBE = "unsubscribe", "Unsubscribed"
        BOUNCE = "bounce", "Hard bounce"
        DECLINED = "declined", "Declined / asked not to be contacted"
        COMPLAINT = "complaint", "Spam complaint"
        MANUAL = "manual", "Added manually"

    email = models.EmailField(blank=True, db_index=True)
    domain = models.CharField(max_length=255, blank=True, db_index=True, help_text="Suppress a whole domain.")
    reason = models.CharField(max_length=12, choices=Reason.choices)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "suppression entries"

    def __str__(self):
        return f"{self.email or '@' + self.domain} ({self.get_reason_display()})"

    @classmethod
    def is_suppressed(cls, email: str) -> bool:
        if not email:
            return False
        email = email.strip().lower()
        domain = email.split("@")[-1]
        return cls.objects.filter(models.Q(email__iexact=email) | models.Q(domain__iexact=domain)).exists()

    @classmethod
    def add(cls, email: str, reason: str, note: str = "", user=None):
        email = (email or "").strip().lower()
        if not email:
            return None
        obj, _ = cls.objects.get_or_create(email=email, defaults={"reason": reason, "note": note[:300], "created_by": user})
        return obj
