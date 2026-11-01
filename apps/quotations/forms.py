from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.products.models import CURRENCIES, INCOTERMS, ProductVariant

from .models import Quotation, QuotationLine


class QuotationCreateForm(BootstrapFormMixin, forms.Form):
    contact = forms.ModelChoiceField(queryset=None, required=False)
    currency = forms.ChoiceField(choices=CURRENCIES, initial="USD")

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].queryset = company.contacts.all() if company else None


class QuotationTermsForm(BootstrapFormMixin, forms.ModelForm):
    incoterm = forms.ChoiceField(choices=[("", "—")] + INCOTERMS, required=False)
    currency = forms.ChoiceField(choices=CURRENCIES)

    class Meta:
        model = Quotation
        fields = ["contact", "currency", "incoterm", "port_of_loading", "destination", "payment_terms", "lead_time",
                  "freight_amount", "freight_note", "packaging", "valid_until", "notes", "internal_notes"]
        widgets = {"valid_until": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}), "notes": forms.Textarea(attrs={"rows": 2}),
                   "internal_notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].queryset = self.instance.company.contacts.all()


class LineForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = QuotationLine
        fields = ["variant", "description", "quantity", "unit", "customer_request"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["variant"].queryset = ProductVariant.objects.filter(
            is_active=True, product__is_verified=True, product__is_active=True).select_related("product")
        self.fields["variant"].label = "Verified product size"
        self.fields["description"].required = False


class DiscountForm(forms.Form):
    discount_percent = forms.DecimalField(min_value=0, max_value=50, decimal_places=2)


class PriceOverrideForm(forms.Form):
    unit_price = forms.DecimalField(min_value=0.0001, decimal_places=4)
