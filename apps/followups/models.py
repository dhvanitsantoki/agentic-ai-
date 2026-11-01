"""Follow-up sequences (configurable schedule) and per-contact enrolments."""
from django.db import models
from django.utils import timezone


class FollowUpSequence(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.CharField(max_length=300, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    @classmethod
    def default(cls):
        return cls.objects.filter(is_default=True, is_active=True).first() or cls.objects.filter(is_active=True).first()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            FollowUpSequence.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)


class FollowUpStep(models.Model):
    sequence = models.ForeignKey(FollowUpSequence, on_delete=models.CASCADE, related_name="steps")
    step_number = models.PositiveSmallIntegerField(help_text="1 = first follow-up after the introduction.")
    day_offset = models.PositiveSmallIntegerField(help_text="Days after the initial e-mail (Day 0).")
    angle = models.CharField(max_length=300, help_text="What this follow-up should add (new value, not repetition).")
    is_final = models.BooleanField(default=False)

    class Meta:
        ordering = ["sequence", "step_number"]
        constraints = [models.UniqueConstraint(fields=["sequence", "step_number"], name="uniq_step_number")]

    def __str__(self):
        return f"{self.sequence} · step {self.step_number} (Day {self.day_offset})"


class Enrollment(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        STOPPED = "stopped", "Stopped"
        COMPLETED = "completed", "Completed"

    class StopReason(models.TextChoices):
        REPLIED = "replied", "Buyer replied"
        DECLINED = "declined", "Buyer declined"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "E-mail bounced"
        STAGE = "stage", "Deal progressed"
        MANUAL = "manual", "Stopped by a user"
        INVALID = "invalid", "Contact no longer e-mailable"

    company = models.ForeignKey("crm.Company", on_delete=models.CASCADE, related_name="enrollments")
    contact = models.ForeignKey("crm.Contact", on_delete=models.CASCADE, related_name="enrollments")
    sequence = models.ForeignKey(FollowUpSequence, on_delete=models.PROTECT, related_name="enrollments")
    initial_email = models.ForeignKey("outreach.OutboundEmail", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    started_at = models.DateTimeField(default=timezone.now, help_text="When the Day-0 e-mail was sent.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    stop_reason = models.CharField(max_length=12, choices=StopReason.choices, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    last_step_done = models.PositiveSmallIntegerField(default=0)
    next_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    history = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["next_due_at"]
        constraints = [
            models.UniqueConstraint(fields=["contact"], condition=models.Q(status="active"), name="one_active_enrollment_per_contact")
        ]

    def __str__(self):
        return f"{self.contact} in {self.sequence} [{self.status}]"

    def log(self, event: str, **data):
        self.history = (self.history or []) + [{"at": timezone.now().isoformat(), "event": event, **data}]

    def stop(self, reason: str, note: str = ""):
        if self.status != self.Status.ACTIVE:
            return False
        self.status = self.Status.STOPPED
        self.stop_reason = reason
        self.stopped_at = timezone.now()
        self.next_due_at = None
        self.log("stopped", reason=reason, note=note)
        self.save(update_fields=["status", "stop_reason", "stopped_at", "next_due_at", "history"])
        return True

    def next_step(self):
        return self.sequence.steps.filter(step_number__gt=self.last_step_done).order_by("step_number").first()
