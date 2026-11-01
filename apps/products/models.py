"""Product database: categories, products, sizes/variants, documents and approved price lists.

Rules:
* Only products marked *verified* by a manager are offered to the AI agents.
* Prices are only ever read from an APPROVED price list valid on the quotation date.
  Approved price lists are immutable; a change needs a new version.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Material(models.TextChoices):
    PP = "PP", "PP"
    PVC = "PVC", "PVC"
    UPVC = "UPVC", "UPVC"
    CPVC = "CPVC", "CPVC"
    RPVC = "RPVC", "RPVC"
    HDPE = "HDPE", "HDPE"
    LDPE = "LDPE", "LDPE"
    ABS = "ABS", "ABS"
    OTHER = "OTHER", "Other"


class ProductCategory(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "product categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:140]
        super().save(*args, **kwargs)


PDF_VALIDATOR = FileExtensionValidator(["pdf"])
DRAWING_VALIDATOR = FileExtensionValidator(["pdf", "png", "jpg", "jpeg", "dwg", "dxf", "svg"])
IMAGE_VALIDATOR = FileExtensionValidator(["png", "jpg", "jpeg", "webp"])
DOCUMENT_VALIDATOR = FileExtensionValidator(["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "png", "jpg", "jpeg", "dwg", "dxf", "zip"])


class Product(models.Model):
    category = models.ForeignKey(ProductCategory, on_delete=models.PROTECT, related_name="products")
    name = models.CharField(max_length=200)
    model_code = models.CharField(max_length=60, blank=True, help_text="Internal model / article code.")
    material = models.CharField(max_length=10, choices=Material.choices)
    description = models.TextField(blank=True)
    applications = models.TextField(blank=True, help_text="e.g. drip irrigation, plumbing, chemical lines.")
    technical_specifications = models.TextField(
        blank=True, help_text="Verified technical data, one item per line (temperature range, body/ball/seal material, ...)."
    )
    available_sizes = models.CharField(max_length=255, blank=True, help_text='Summary, e.g. 1/2" to 4" / 20–110 mm.')
    standards = models.CharField(max_length=255, blank=True, help_text="Standards the product actually conforms to.")
    pressure_rating = models.CharField(max_length=120, blank=True, help_text="e.g. PN10 / 150 psi at 23 °C.")
    connection_types = models.CharField(max_length=255, blank=True, help_text="e.g. solvent socket, BSP thread, NPT thread.")
    colours = models.CharField(max_length=120, blank=True)
    default_moq = models.PositiveIntegerField(null=True, blank=True, help_text="Minimum order quantity (pieces).")
    packaging_details = models.CharField(max_length=255, blank=True)
    sample_available = models.BooleanField(default=False)
    oem_customisable = models.BooleanField(
        default=False, help_text="Tick only if private-label / custom colour / branding is actually offered for this product."
    )
    catalogue_pdf = models.FileField(upload_to="products/catalogues/", blank=True, validators=[PDF_VALIDATOR])
    technical_drawing = models.FileField(upload_to="products/drawings/", blank=True, validators=[DRAWING_VALIDATOR])

    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(
        default=False, help_text="Verified by a manager. Unverified products are never used by the AI agents."
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category__sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["category", "name", "material"], name="uniq_product_name_per_category")
        ]

    def __str__(self):
        return f"{self.name} ({self.material})"

    def mark_verified(self, user):
        self.is_verified = True
        self.verified_by = user
        self.verified_at = timezone.now()
        self.save(update_fields=["is_verified", "verified_by", "verified_at", "updated_at"])

    def missing_fields(self) -> list[str]:
        checks = {
            "Technical specifications": self.technical_specifications,
            "Available sizes": self.available_sizes or self.variants.exists(),
            "Pressure rating": self.pressure_rating,
            "Connection types": self.connection_types,
            "MOQ": self.default_moq or self.variants.filter(moq__isnull=False).exists(),
            "Packaging": self.packaging_details,
            "Catalogue PDF": self.catalogue_pdf,
            "Images": self.images.exists(),
        }
        return [label for label, value in checks.items() if not value]

    def summary_for_ai(self) -> dict:
        return {
            "id": self.pk,
            "name": self.name,
            "category": self.category.name,
            "material": self.material,
            "sizes": self.available_sizes or ", ".join(v.size for v in self.variants.filter(is_active=True)),
            "pressure_rating": self.pressure_rating,
            "connection_types": self.connection_types,
            "standards": self.standards,
            "applications": self.applications,
            "oem_customisable": self.oem_customisable,
            "sample_available": self.sample_available,
        }


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    image = models.FileField(upload_to="products/images/", validators=[IMAGE_VALIDATOR])
    caption = models.CharField(max_length=200, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.caption or self.image.name


class ProductVariant(models.Model):
    """A sellable size/configuration of a product (the unit that carries a price)."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    sku = models.CharField("SKU / article no.", max_length=80, unique=True)
    size = models.CharField(max_length=40, help_text='e.g. 1/2", 20 mm, 63 mm x 1"')
    size_mm = models.DecimalField(max_digits=7, decimal_places=1, null=True, blank=True, help_text="Nominal size in mm (for matching).")
    pressure_rating = models.CharField(max_length=60, blank=True, help_text="Leave blank to use the product's rating.")
    connection_type = models.CharField(max_length=80, blank=True)
    moq = models.PositiveIntegerField(null=True, blank=True)
    packaging = models.CharField(max_length=120, blank=True, help_text="e.g. 50 pcs per carton")
    unit_weight_g = models.DecimalField(max_digits=9, decimal_places=1, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["product", "size_mm", "size"]

    def __str__(self):
        return f"{self.product.name} {self.size} [{self.sku}]"

    @property
    def effective_pressure_rating(self):
        return self.pressure_rating or self.product.pressure_rating

    @property
    def effective_moq(self):
        return self.moq if self.moq is not None else self.product.default_moq

    @property
    def effective_packaging(self):
        return self.packaging or self.product.packaging_details


class DocumentKind(models.TextChoices):
    CATALOGUE = "catalogue", "Product catalogue"
    BROCHURE = "brochure", "Company brochure"
    TECHNICAL = "technical", "Technical data sheet"
    DRAWING = "drawing", "Technical drawing"
    CERTIFICATE = "certificate", "Certificate / test report"
    PRICE = "price", "Price list (internal)"
    OTHER = "other", "Other"


class CompanyDocument(models.Model):
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=DocumentKind.choices, default=DocumentKind.CATALOGUE)
    file = models.FileField(upload_to="documents/", validators=[DOCUMENT_VALIDATOR])
    categories = models.ManyToManyField(ProductCategory, blank=True, related_name="documents")
    products = models.ManyToManyField(Product, blank=True, related_name="documents")
    approved_for_sending = models.BooleanField(
        default=False, help_text="Only approved documents can be attached to e-mails by the agents."
    )
    notes = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "title"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.kind == DocumentKind.PRICE and self.approved_for_sending:
            raise ValidationError("Internal price lists cannot be approved for automatic sending.")

    @property
    def filename(self):
        return self.file.name.rsplit("/", 1)[-1] if self.file else ""


CURRENCIES = [("USD", "USD"), ("EUR", "EUR"), ("INR", "INR"), ("GBP", "GBP"), ("AED", "AED"), ("AUD", "AUD")]
INCOTERMS = [
    ("EXW", "EXW – Ex Works"), ("FCA", "FCA – Free Carrier"), ("FOB", "FOB – Free On Board"),
    ("CFR", "CFR – Cost and Freight"), ("CIF", "CIF – Cost, Insurance and Freight"),
    ("CPT", "CPT – Carriage Paid To"), ("CIP", "CIP – Carriage and Insurance Paid To"),
    ("DAP", "DAP – Delivered At Place"), ("DDP", "DDP – Delivered Duty Paid"),
]


class PriceList(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        ARCHIVED = "archived", "Archived"

    name = models.CharField(max_length=120)
    currency = models.CharField(max_length=3, choices=CURRENCIES, default="USD")
    incoterm_basis = models.CharField(max_length=3, choices=INCOTERMS, default="FOB",
                                      help_text="The Incoterm these prices are based on.")
    valid_from = models.DateField(default=timezone.localdate)
    valid_until = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-valid_from", "name"]

    def __str__(self):
        return f"{self.name} ({self.currency}, {self.get_status_display()})"

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT

    def is_valid_on(self, day) -> bool:
        return (
            self.status == self.Status.APPROVED
            and self.valid_from <= day
            and (self.valid_until is None or day <= self.valid_until)
        )

    def approve(self, user):
        if not self.items.exists():
            raise ValidationError("A price list without items cannot be approved.")
        self.status = self.Status.APPROVED
        self.approved_by = user
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approved_by", "approved_at"])


class PriceListItem(models.Model):
    price_list = models.ForeignKey(PriceList, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="price_items")
    unit_price = models.DecimalField(max_digits=12, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    min_quantity = models.PositiveIntegerField(default=1, help_text="Price applies from this quantity (quantity breaks).")

    class Meta:
        ordering = ["variant", "min_quantity"]
        constraints = [
            models.UniqueConstraint(fields=["price_list", "variant", "min_quantity"], name="uniq_price_break")
        ]

    def __str__(self):
        return f"{self.variant} ≥{self.min_quantity}: {self.unit_price} {self.price_list.currency}"

    def save(self, *args, **kwargs):
        if self.price_list_id and not self.price_list.is_editable:
            raise ValidationError("Approved or archived price lists cannot be changed. Create a new version.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not self.price_list.is_editable:
            raise ValidationError("Approved or archived price lists cannot be changed.")
        return super().delete(*args, **kwargs)
