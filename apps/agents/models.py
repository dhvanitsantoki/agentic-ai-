"""Agent infrastructure: job queue, scheduled tasks and the agent-run log (tokens, cost, errors)."""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class AgentName(models.TextChoices):
    BUYER_RESEARCH = "buyer_research", "1 · Global buyer research"
    OEM_RESEARCH = "oem_research", "2 · OEM customer research"
    OUTREACH = "outreach", "3 · E-mail outreach"
    MAILBOX = "mailbox", "4 · E-mail management"
    FOLLOWUP = "followup", "5 · Follow-up"
    QUOTATION = "quotation", "6 · Quotation & commercial"
    CRM = "crm", "7 · CRM & pipeline"
    REPORT = "report", "8 · Management report"
    COUNTRY = "country", "Country market research"
    SYSTEM = "system", "System"


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    kind = models.CharField(max_length=60, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    priority = models.SmallIntegerField(default=5, help_text="Lower runs first.")
    run_after = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    last_error = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    locked_by = models.CharField(max_length=80, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    scheduled_task = models.ForeignKey("ScheduledTask", null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "priority", "run_after"])]

    def __str__(self):
        return f"Job #{self.pk} {self.kind} [{self.status}]"


class ScheduledTask(models.Model):
    name = models.CharField(max_length=120, unique=True)
    job_kind = models.CharField(max_length=60)
    cron = models.CharField(max_length=60, help_text="Cron expression in the server time zone, e.g. '0 2 * * *'.")
    payload = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    description = models.CharField(max_length=300, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.cron})"


class AgentRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    agent = models.CharField(max_length=20, choices=AgentName.choices, db_index=True)
    action = models.CharField(max_length=80)
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL, related_name="agent_runs")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING, db_index=True)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=40, blank=True)
    summary = models.TextField(blank=True)
    error = models.TextField(blank=True)
    model = models.CharField(max_length=60, blank=True)
    llm_calls = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    web_searches = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.get_agent_display()} · {self.action} [{self.status}]"

    @property
    def duration_seconds(self):
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None
