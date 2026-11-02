"""Bulk CSV import of products, sizes and prices (column lists double as downloadable templates)."""
from django.db import transaction

from .forms import parse_decimal
from .models import Material, Product, ProductCategory, ProductVariant

PRODUCT_COLUMNS = [
    "category", "name", "material", "model_code", "description", "applications", "technical_specifications",
    "available_sizes", "standards", "pressure_rating", "connection_types", "colours", "default_moq", "packaging_details",
    "sample_available", "oem_customisable",
]
SIZE_COLUMNS = ["product_name", "material", "sku", "size", "size_mm", "pressure_rating", "connection_type", "moq",
                "packaging", "unit_weight_g"]
VARIANT_COLUMNS = SIZE_COLUMNS[2:]
PRICE_COLUMNS = ["sku", "unit_price", "min_quantity"]
TECHNICAL_FIELDS = ("material", "technical_specifications", "standards", "pressure_rating", "connection_types")
TEXT_FIELDS = ("model_code", "description", "applications", "technical_specifications", "available_sizes", "standards",
               "pressure_rating", "connection_types", "colours", "packaging_details")
MATERIALS = {m.value for m in Material}


def _bool(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "yes", "y", "true", "ha", "haa"}


def _int(value: str):
    v = (value or "").replace(",", "").strip()
    return int(v) if v.isdigit() else None


@transaction.atomic
def import_products(rows: list[dict]) -> tuple[int, int, list[str]]:
    """Create/update products keyed by (category, name, material). Technical changes reset verification."""
    created = updated = 0
    errors = []
    categories = {c.name.lower(): c for c in ProductCategory.objects.all()}
    for i, row in enumerate(rows, start=2):
        cat = categories.get((row.get("category") or "").strip().lower())
        name = (row.get("name") or "").strip()
        material = (row.get("material") or "").strip().upper()
        if not cat:
            errors.append(f"Row {i}: unknown category '{row.get('category', '')}' (use the exact category names)")
            continue
        if not name:
            errors.append(f"Row {i}: name is required")
            continue
        if material not in MATERIALS:
            errors.append(f"Row {i}: material must be one of {', '.join(sorted(MATERIALS))}")
            continue
        values = {f: (row.get(f) or "").strip()[:255 if f in ("available_sizes", "standards", "connection_types", "packaging_details") else 10000]
                  for f in TEXT_FIELDS}
        values["model_code"] = values["model_code"][:60]
        values["pressure_rating"] = values["pressure_rating"][:120]
        values["colours"] = values["colours"][:120]
        values["default_moq"] = _int(row.get("default_moq", ""))
        values["sample_available"] = _bool(row.get("sample_available", ""))
        values["oem_customisable"] = _bool(row.get("oem_customisable", ""))
        product = Product.objects.filter(category=cat, name=name, material=material).first()
        if product is None:
            Product.objects.create(category=cat, name=name, material=material, **values)
            created += 1
            continue
        technical_change = any(str(getattr(product, f) or "") != str(values.get(f, material) or "") for f in TECHNICAL_FIELDS if f != "material")
        for k, v in values.items():
            setattr(product, k, v)
        if technical_change and product.is_verified:
            product.is_verified = False
        product.save()
        updated += 1
    return created, updated, errors


@transaction.atomic
def import_sizes(rows: list[dict], product: Product | None = None) -> tuple[int, int, list[str]]:
    """Create/update sizes keyed by SKU. Without `product`, each row names its product (product_name + material)."""
    created = updated = 0
    errors = []
    for i, row in enumerate(rows, start=2):
        target = product
        if target is None:
            name = (row.get("product_name") or "").strip()
            material = (row.get("material") or "").strip().upper()
            matches = Product.objects.filter(name__iexact=name, **({"material": material} if material else {}))
            if matches.count() != 1:
                errors.append(f"Row {i}: product '{name}' {material} {'not found' if not matches.exists() else 'is ambiguous - add material'}")
                continue
            target = matches.get()
        sku, size = (row.get("sku") or "").strip(), (row.get("size") or "").strip()
        if not sku or not size:
            errors.append(f"Row {i}: sku and size are required")
            continue
        existing = ProductVariant.objects.filter(sku=sku).first()
        if existing and existing.product_id != target.pk:
            errors.append(f"Row {i}: SKU {sku} belongs to another product ({existing.product.name})")
            continue
        defaults = {
            "product": target, "size": size[:40], "is_active": True,
            "size_mm": parse_decimal(row["size_mm"]) if row.get("size_mm") else None,
            "pressure_rating": (row.get("pressure_rating") or "")[:60], "connection_type": (row.get("connection_type") or "")[:80],
            "moq": _int(row.get("moq", "")), "packaging": (row.get("packaging") or "")[:120],
            "unit_weight_g": parse_decimal(row["unit_weight_g"]) if row.get("unit_weight_g") else None,
        }
        _, was_created = ProductVariant.objects.update_or_create(sku=sku, defaults=defaults)
        created += was_created
        updated += not was_created
    return created, updated, errors
