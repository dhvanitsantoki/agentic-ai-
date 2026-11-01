"""Quotations built only from approved price lists, with mandatory human approval."""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Quotation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        NEEDS_INPUT = "needs_input", "Needs human input"
        PENDING_APPROVAL = "pending_approval", "Waiting for manager approval"
        APPROVED = "approved", "Approved"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected by buyer"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    number = models.CharField(max_length=40, unique=True, blank=True)
    revision = models.PositiveSmallIntegerField(default=0)
    company = models.ForeignKey("crm.Company", on_delete=models.PROTECT, related_name="quotations")
    contact = models.ForeignKey("crm.Contact", null=True, blank=True, on_delete=models.SET_NULL)
    currency = models.CharField(max_length=3, default="USD")
    incoterm = models.CharField(max_length=3, blank=True)
    port_of_loading = models.CharField(max_length=120, blank=True)
    destination = models.CharField(max_length=160, blank=True, help_text="Destination port / place.")
    payment_terms = models.CharField(max_length=255, blank=True)
    lead_time = models.CharField(max_length=160, blank=True)
    freight_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    freight_note = models.CharField(max_length=255, blank=True, help_text="e.g. 'Freight to be confirmed at booking' or 'Included'.")
    packaging = models.CharField(max_length=255, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("50"))],
        help_text="Manager-only. Applied to the goods subtotal.",
    )
    discount_set_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.TextField(blank=True, help_text="Printed on the quotation.")
    internal_notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    missing_fields = models.JSONField(default=list, blank=True)
    source_email = models.ForeignKey("mailbox.InboundEmail", null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations")
    requirement = models.ForeignKey("crm.Requirement", null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by_agent = models.BooleanField(default=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    pdf = models.FileField(upload_to="quotations/", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.number or f"Draft quotation #{self.pk}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.number:
            year = (self.created_at or timezone.now()).year
            self.number = f"FLT/Q/{year}/{self.pk:05d}"
            super().save(update_fields=["number"])

    @property
    def is_locked(self):
        return self.status in (self.Status.APPROVED, self.Status.SENT, self.Status.ACCEPTED, self.Status.REJECTED, self.Status.EXPIRED)

    @property
    def subtotal(self) -> Decimal:
        return sum((line.line_total or Decimal("0")) for line in self.lines.all())

    @property
    def discount_amount(self) -> Decimal:
        return (self.subtotal * self.discount_percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def total(self) -> Decimal:
        return self.subtotal - self.discount_amount + (self.freight_amount or Decimal("0"))


class QuotationLine(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveSmallIntegerField(default=1)
    variant = models.ForeignKey("products.ProductVariant", null=True, blank=True, on_delete=models.PROTECT)
    description = models.CharField(max_length=300)
    size = models.CharField(max_length=60, blank=True)
    material = models.CharField(max_length=20, blank=True)
    pressure_rating = models.CharField(max_length=80, blank=True)
    connection_type = models.CharField(max_length=120, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=20, default="pcs")
    unit_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    price_item = models.ForeignKey("products.PriceListItem", null=True, blank=True, on_delete=models.SET_NULL,
                                   help_text="Approved price-list row the unit price came from.")
    price_overridden_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    moq = models.PositiveIntegerField(null=True, blank=True)
    packaging = models.CharField(max_length=160, blank=True)
    customer_request = models.CharField(max_length=500, blank=True, help_text="What the buyer asked for (original wording).")

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.description} x {self.quantity}"

    @property
    def line_total(self):
        if self.unit_price is None:
            return None
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def below_moq(self):
        return self.moq is not None and self.quantity < self.moq
