"""Connected mailboxes (OAuth) and received e-mails with AI classification."""
from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.crypto import decrypt, encrypt


class Provider(models.TextChoices):
    GMAIL = "gmail", "Gmail / Google Workspace"
    OUTLOOK = "outlook", "Microsoft 365 / Outlook"
    ZOHO = "zoho", "Zoho Mail"


class MailAccount(models.Model):
    class Status(models.TextChoices):
        CONNECTED = "connected", "Connected"
        ERROR = "error", "Error"
        REVOKED = "revoked", "Disconnected"

    provider = models.CharField(max_length=10, choices=Provider.choices)
    email_address = models.EmailField()
    display_name = models.CharField(max_length=120, blank=True, help_text="Sender name shown to buyers.")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="mail_accounts")
    provider_account_id = models.CharField(max_length=120, blank=True, help_text="Zoho accountId / Graph user id.")
    provider_data = models.JSONField(default=dict, blank=True)
    _access_token = models.TextField(db_column="access_token", blank=True)
    _refresh_token = models.TextField(db_column="refresh_token", blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CONNECTED)
    last_error = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, help_text="Inactive accounts are neither synced nor used for sending.")
    use_for_sending = models.BooleanField(default=True)
    is_default_sender = models.BooleanField(default=False)
    daily_limit = models.PositiveIntegerField(null=True, blank=True, help_text="Override the global per-mailbox daily limit.")
    sync_cursor = models.CharField(max_length=500, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-is_default_sender", "email_address"]
        constraints = [models.UniqueConstraint(fields=["provider", "email_address"], name="uniq_mailbox")]

    def __str__(self):
        return f"{self.email_address} ({self.get_provider_display()})"

    @property
    def access_token(self):
        return decrypt(self._access_token)

    @access_token.setter
    def access_token(self, value):
        self._access_token = encrypt(value or "")

    @property
    def refresh_token(self):
        return decrypt(self._refresh_token)

    @refresh_token.setter
    def refresh_token(self, value):
        self._refresh_token = encrypt(value or "")

    @property
    def token_is_expired(self):
        return not self.token_expires_at or self.token_expires_at <= timezone.now() + timezone.timedelta(seconds=60)

    @property
    def sender_header(self):
        from email.utils import formataddr

        return formataddr((self.display_name, self.email_address)) if self.display_name else self.email_address


class Classification(models.TextChoices):
    INTERESTED = "interested", "Interested"
    NEED_QUOTATION = "need_quotation", "Need quotation"
    NEED_CATALOGUE = "need_catalogue", "Need catalogue"
    NEED_TECHNICAL = "need_technical", "Need technical details"
    NEGOTIATION = "negotiation", "Negotiation"
    NOT_INTERESTED = "not_interested", "Not interested"
    NO_RESPONSE = "no_response", "No response"
    FUTURE = "future", "Future opportunity"
    EXISTING_CUSTOMER = "existing_customer", "Existing customer"
    COMPLAINT = "complaint", "Complaint"
    BOUNCE = "bounce", "Bounce / delivery failure"
    AUTO_REPLY = "auto_reply", "Auto-reply / out of office"
    UNSUBSCRIBE = "unsubscribe", "Unsubscribe request"
    UNRELATED = "unrelated", "Unrelated / spam"
    PENDING = "pending", "Not yet classified"


ESCALATE_CLASSES = {
    Classification.INTERESTED, Classification.NEED_QUOTATION, Classification.NEGOTIATION,
    Classification.COMPLAINT, Classification.EXISTING_CUSTOMER, Classification.NEED_TECHNICAL,
}


class InboundEmail(models.Model):
    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="inbound")
    provider_message_id = models.CharField(max_length=255)
    provider_thread_id = models.CharField(max_length=255, blank=True, db_index=True)
    provider_folder_id = models.CharField(max_length=120, blank=True)
    rfc_message_id = models.CharField(max_length=500, blank=True, db_index=True)
    in_reply_to = models.CharField(max_length=500, blank=True)
    references = models.TextField(blank=True)
    from_email = models.EmailField(db_index=True)
    from_name = models.CharField(max_length=200, blank=True)
    to_emails = models.TextField(blank=True)
    cc_emails = models.TextField(blank=True)
    subject = models.CharField(max_length=500, blank=True)
    body_text = models.TextField(blank=True)
    headers = models.JSONField(default=dict, blank=True)
    attachment_names = models.JSONField(default=list, blank=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    company = models.ForeignKey("crm.Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="inbound_emails")
    contact = models.ForeignKey("crm.Contact", null=True, blank=True, on_delete=models.SET_NULL, related_name="inbound_emails")
    matched_outbound = models.ForeignKey("outreach.OutboundEmail", null=True, blank=True, on_delete=models.SET_NULL, related_name="replies")

    classification = models.CharField(max_length=20, choices=Classification.choices, default=Classification.PENDING, db_index=True)
    classification_confidence = models.FloatField(null=True, blank=True)
    classified_by = models.CharField(max_length=20, blank=True, help_text="rules / ai / human")
    summary = models.TextField(blank=True)
    extracted = models.JSONField(default=dict, blank=True, help_text="Requirements, questions, sizes, quantities...")
    is_urgent = models.BooleanField(default=False)
    needs_attention = models.BooleanField(default=False, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_error = models.TextField(blank=True)
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [models.UniqueConstraint(fields=["account", "provider_message_id"], name="uniq_inbound_per_account")]

    def __str__(self):
        return f"{self.from_email}: {self.subject}"
