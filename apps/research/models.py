"""Research runs (Agents 1 & 2, country research) and their individual findings."""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class ResearchRun(models.Model):
    class Kind(models.TextChoices):
        BUYER = "buyer", "Global buyer research"
        OEM = "oem", "OEM customer research"
        COUNTRY = "country", "Country market research"
        DEEP_DIVE = "deep_dive", "Company deep-dive"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    countries = models.ManyToManyField("geo.Country", blank=True, related_name="research_runs")
    segments = models.JSONField(default=list, blank=True, help_text="Target industries (crm.Industry values).")
    product_focus = models.ManyToManyField("products.ProductCategory", blank=True, related_name="research_runs")
    company_types = models.JSONField(default=list, blank=True)
    target_count = models.PositiveSmallIntegerField(default=8)
    instructions = models.TextField(blank=True, help_text="Optional extra guidance for the agent.")
    company = models.ForeignKey("crm.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="deep_dives")
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    triggered_by_schedule = models.BooleanField(default=False)
    job = models.ForeignKey("agents.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    summary = models.TextField(blank=True)
    error = models.TextField(blank=True)
    found_count = models.PositiveIntegerField(default=0)
    added_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    review_count = models.PositiveIntegerField(default=0)
    verified_contacts = models.PositiveIntegerField(default=0)
    unverified_contacts = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    web_searches = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk}"

    @property
    def country_names(self):
        return ", ".join(c.name for c in self.countries.all())


class ResearchFinding(models.Model):
    class Status(models.TextChoices):
        ADDED = "added", "Added to CRM"
        UPDATED = "updated", "Merged into existing company"
        DUPLICATE = "duplicate", "Duplicate (skipped)"
        NEEDS_REVIEW = "needs_review", "Needs human review"
        REJECTED = "rejected", "Rejected"

    run = models.ForeignKey(ResearchRun, on_delete=models.CASCADE, related_name="findings")
    name = models.CharField(max_length=255)
    website = models.URLField(max_length=500, blank=True)
    country_code = models.CharField(max_length=2, blank=True)
    raw = models.JSONField(default=dict, help_text="Exactly what the AI returned.")
    verification = models.JSONField(default=dict, blank=True, help_text="Deterministic verification results.")
    status = models.CharField(max_length=15, choices=Status.choices, db_index=True)
    reason = models.TextField(blank=True)
    company = models.ForeignKey("crm.Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="findings")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} [{self.get_status_display()}]"
