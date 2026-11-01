from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.crm.models import CompanyType, Industry
from apps.geo.models import Continent, Country
from apps.products.models import ProductCategory


def grouped_country_choices():
    groups = []
    for continent in Continent.objects.prefetch_related("countries"):
        groups.append((continent.name, [(c.iso2, c.name) for c in continent.countries.all().order_by("name")]))
    return groups


class ResearchRunForm(BootstrapFormMixin, forms.Form):
    kind = forms.ChoiceField(choices=[("buyer", "Global buyer research (importers, distributors, wholesalers…)"),
                                      ("oem", "OEM customer research (own-brand, private-label, outsourcing)")])
    countries = forms.MultipleChoiceField(help_text="Hold Ctrl/Cmd to select several. Fewer countries per run gives better results.")
    segments = forms.MultipleChoiceField(choices=Industry.choices, required=False, widget=forms.CheckboxSelectMultiple)
    company_types = forms.MultipleChoiceField(choices=CompanyType.choices, required=False, widget=forms.CheckboxSelectMultiple)
    product_focus = forms.ModelMultipleChoiceField(queryset=ProductCategory.objects.all(), required=False,
                                                   widget=forms.CheckboxSelectMultiple)
    target_count = forms.IntegerField(min_value=1, max_value=25, initial=8, help_text="Maximum companies to research in this run.")
    instructions = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}),
                                   help_text="Optional, e.g. 'focus on drip irrigation distributors in the Rift Valley'.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["countries"].choices = grouped_country_choices()
        self.fields["countries"].widget.attrs.update({"size": 12})

    def clean_countries(self):
        codes = self.cleaned_data["countries"]
        if len(codes) > 10:
            raise forms.ValidationError("Select at most 10 countries per run.")
        return list(Country.objects.filter(iso2__in=codes))
