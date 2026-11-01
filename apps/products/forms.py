import csv
import io
from decimal import Decimal, InvalidOperation

from django import forms

from apps.core.forms import BootstrapFormMixin

from .models import CompanyDocument, PriceList, PriceListItem, Product, ProductCategory, ProductImage, ProductVariant


class CategoryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ProductCategory
        fields = ["name", "description", "sort_order"]


class ProductForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Product
        exclude = ["is_verified", "verified_by", "verified_at"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "applications": forms.Textarea(attrs={"rows": 2}),
            "technical_specifications": forms.Textarea(attrs={"rows": 4}),
        }


class VariantForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ProductVariant
        exclude = ["product"]


class VariantAddForm(BootstrapFormMixin, forms.ModelForm):
    """Inline 'add size' form on the product page (new sizes are always active)."""

    class Meta:
        model = ProductVariant
        fields = ["sku", "size", "size_mm", "pressure_rating", "connection_type", "moq", "packaging"]


class ImageForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ProductImage
        fields = ["image", "caption"]


class DocumentForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CompanyDocument
        fields = ["title", "kind", "file", "categories", "products", "approved_for_sending", "notes"]
        widgets = {"categories": forms.CheckboxSelectMultiple, "products": forms.SelectMultiple(attrs={"size": 6})}


class PriceListForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = PriceList
        fields = ["name", "currency", "incoterm_basis", "valid_from", "valid_until", "notes"]
        widgets = {
            "valid_from": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "valid_until": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        data = super().clean()
        if data.get("valid_until") and data.get("valid_from") and data["valid_until"] < data["valid_from"]:
            self.add_error("valid_until", "Must be on or after the start date.")
        return data


class PriceItemForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = PriceListItem
        fields = ["variant", "unit_price", "min_quantity"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["variant"].queryset = ProductVariant.objects.filter(is_active=True).select_related("product")


class CsvUploadForm(BootstrapFormMixin, forms.Form):
    file = forms.FileField(help_text="UTF-8 CSV file with a header row.")

    def read_rows(self) -> list[dict]:
        raw = self.cleaned_data["file"].read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise forms.ValidationError("The file must be UTF-8 encoded CSV.") from exc
        reader = csv.DictReader(io.StringIO(text))
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]


def parse_decimal(value: str):
    try:
        return Decimal(value.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
