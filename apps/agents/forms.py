from django import forms

from apps.core.forms import BootstrapFormMixin

from .models import ScheduledTask
from .scheduler import is_valid_cron


class ScheduledTaskForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ScheduledTask
        fields = ["cron", "enabled", "description"]

    def clean_cron(self):
        cron = self.cleaned_data["cron"].strip()
        if not is_valid_cron(cron):
            raise forms.ValidationError("Invalid cron expression (minute hour day month weekday).")
        return cron
