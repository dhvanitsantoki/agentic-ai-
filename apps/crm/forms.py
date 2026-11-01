from django import forms
from django.contrib.auth import get_user_model

from apps.core.forms import BootstrapFormMixin
from apps.geo.models import Country

from .models import Activity, Company, Contact, Industry, Requirement, Stage, Task


class CompanyForm(BootstrapFormMixin, forms.ModelForm):
    industries = forms.MultipleChoiceField(choices=Industry.choices, required=False, widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = Company
        fields = [
            "name", "website", "country", "city", "address", "phone", "general_email", "linkedin_url",
            "company_type", "industries", "business_type", "description", "existing_brands", "product_range",
            "potential_requirements", "products_interested", "estimated_size", "estimated_size_source",
            "owner", "lead_source", "is_existing_customer", "do_not_contact",
            "expected_order_quantity", "expected_order_value", "expected_value_currency", "negotiation_status",
            "next_action", "next_action_date", "notes",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "product_range": forms.Textarea(attrs={"rows": 2}),
            "potential_requirements": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "next_action_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "products_interested": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["country"].queryset = Country.objects.select_related("continent").order_by("name")
        self.fields["owner"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")


class ContactForm(BootstrapFormMixin, forms.ModelForm):
    confirm_email = forms.BooleanField(
        required=False, label="I confirm this e-mail address is correct (e.g. from a business card or the buyer's own e-mail).",
        help_text="Unconfirmed addresses must be verified before any e-mail is sent.",
    )

    class Meta:
        model = Contact
        fields = ["full_name", "title", "department", "is_decision_maker", "is_primary", "email", "email_source_url",
                  "phone", "whatsapp", "phone_source_url", "linkedin_url", "do_not_contact", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class ActivityForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Activity
        fields = ["kind", "contact", "subject", "body", "occurred_at"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3}), "occurred_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = [(k, l) for k, l in Activity.Kind.choices if k in ("call", "whatsapp", "meeting", "note", "sample")]
        self.fields["contact"].queryset = company.contacts.all() if company else Contact.objects.none()
        self.fields["occurred_at"].required = False


class RequirementForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Requirement
        fields = ["contact", "product_category", "description", "material", "sizes", "pressure_rating", "connection_type",
                  "quantity", "target_price", "branding", "status"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].queryset = company.contacts.all() if company else Contact.objects.none()


class TaskForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "company", "details", "priority", "assigned_to", "due_date"]
        widgets = {"details": forms.Textarea(attrs={"rows": 2}), "due_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = get_user_model().objects.filter(is_active=True)
        self.fields["company"].queryset = Company.objects.order_by("name")


class StageForm(forms.Form):
    stage = forms.ChoiceField(choices=Stage.choices)
    reason = forms.CharField(required=False, max_length=300)


class CompanyImportForm(BootstrapFormMixin, forms.Form):
    file = forms.FileField(help_text="UTF-8 CSV. Columns: name, website, country (ISO code or name), city, company_type, "
                                     "contact_name, contact_title, email, phone, source_url, notes")
    lead_source = forms.ChoiceField(choices=[("import", "CSV import"), ("exhibition", "Exhibition"), ("referral", "Referral"),
                                             ("existing", "Existing customer")])
    emails_confirmed = forms.BooleanField(
        required=False, label="The e-mail addresses in this file were obtained directly (business cards, exhibition scans, existing customers).",
        help_text="If not ticked, imported addresses stay unverified until the verification check passes.",
    )
    run_verification = forms.BooleanField(required=False, initial=True,
                                          label="Run website / e-mail verification on imported companies (background job)")
