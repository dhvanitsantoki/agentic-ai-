from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from .models import CompanyProfile, Role, SystemSettings, UserProfile


class BootstrapFormMixin:
    """Adds Bootstrap classes to every widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput,)):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", "form-select")
            elif isinstance(widget, forms.CheckboxSelectMultiple):
                pass
            else:
                widget.attrs.setdefault("class", "form-control")
            if isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", 3)


class CompanyProfileForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CompanyProfile
        exclude = ["updated_by"]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
            "about": forms.Textarea(attrs={"rows": 4}),
            "approved_facts": forms.Textarea(attrs={"rows": 5}),
            "manufacturing_capabilities": forms.Textarea(attrs={"rows": 4}),
            "certifications": forms.Textarea(attrs={"rows": 3}),
            "oem_services": forms.Textarea(attrs={"rows": 3}),
            "quotation_terms": forms.Textarea(attrs={"rows": 4}),
        }


AUTO_SEND_KIND_CHOICES = [
    ("initial", "Initial introduction e-mails (from approved templates)"),
    ("followup", "Scheduled follow-ups"),
    ("catalogue", "Catalogue replies (only when the reply contains no prices or technical claims)"),
]


class SystemSettingsForm(BootstrapFormMixin, forms.ModelForm):
    auto_send_kinds = forms.MultipleChoiceField(
        choices=AUTO_SEND_KIND_CHOICES, required=False, widget=forms.CheckboxSelectMultiple,
        help_text="Only used in Mode 3. Anything involving prices, discounts, quotations, payment terms, "
        "exclusivity, legal terms, certifications or technical claims always needs a human.",
    )

    class Meta:
        model = SystemSettings
        fields = [
            "execution_mode", "auto_send_kinds", "daily_send_limit", "hourly_send_limit",
            "send_window_start_hour", "send_window_end_hour", "skip_weekends",
            "min_days_between_initial_emails", "unsubscribe_footer",
            "research_daily_company_limit", "research_companies_per_run", "research_max_web_searches",
            "ai_model_research", "ai_effort_research", "ai_model_drafting", "ai_effort_drafting",
            "ai_model_classification", "ai_effort_classification", "monthly_ai_budget_usd",
            "require_two_factor", "escalation_emails", "report_recipients",
        ]
        widgets = {"unsubscribe_footer": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        data = super().clean()
        start, end = data.get("send_window_start_hour"), data.get("send_window_end_hour")
        if start is not None and end is not None:
            if not (0 <= start < end <= 24):
                self.add_error("send_window_end_hour", "Window must satisfy 0 ≤ start < end ≤ 24.")
        footer = data.get("unsubscribe_footer") or ""
        if "{unsubscribe_url}" not in footer:
            self.add_error("unsubscribe_footer", "The footer must contain {unsubscribe_url}.")
        return data


User = get_user_model()


class UserCreateForm(BootstrapFormMixin, forms.ModelForm):
    role = forms.ChoiceField(choices=Role.choices)
    password = forms.CharField(widget=forms.PasswordInput, help_text="At least 10 characters.")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email"]

    def clean_password(self):
        pwd = self.cleaned_data["password"]
        validate_password(pwd)
        return pwd

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        user.save()
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = self.cleaned_data["role"]
        profile.save()
        return user


class UserEditForm(BootstrapFormMixin, forms.ModelForm):
    role = forms.ChoiceField(choices=Role.choices)
    job_title = forms.CharField(required=False)
    phone = forms.CharField(required=False)
    email_signature = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        profile = getattr(self.instance, "profile", None)
        if profile:
            self.fields["role"].initial = profile.role
            self.fields["job_title"].initial = profile.job_title
            self.fields["phone"].initial = profile.phone
            self.fields["email_signature"].initial = profile.email_signature

    def save(self, commit=True):
        user = super().save()
        profile, _ = UserProfile.objects.get_or_create(user=user)
        for f in ("role", "job_title", "phone", "email_signature"):
            setattr(profile, f, self.cleaned_data.get(f) or "")
        profile.save()
        return user
