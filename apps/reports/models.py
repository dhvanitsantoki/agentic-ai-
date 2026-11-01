from django.db import models


class ReportSnapshot(models.Model):
    class Period(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    period = models.CharField(max_length=10, choices=Period.choices)
    start = models.DateField()
    end = models.DateField(help_text="Inclusive.")
    metrics = models.JSONField(default=dict)
    narrative = models.TextField(blank=True, help_text="AI summary written only from the metrics above.")
    generated_at = models.DateTimeField(auto_now=True)
    emailed_to = models.TextField(blank=True)

    class Meta:
        ordering = ["-start", "period"]
        constraints = [models.UniqueConstraint(fields=["period", "start"], name="uniq_report_period")]

    def __str__(self):
        return f"{self.get_period_display()} report {self.start}"
