"""Pricing lookups and product matching. Prices come ONLY from approved, in-date price lists."""
import re
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from .models import PriceList, PriceListItem, Product, ProductVariant


@dataclass
class PriceResult:
    unit_price: Decimal
    item: PriceListItem
    price_list: PriceList


def get_approved_price(variant: ProductVariant, currency: str, quantity: int = 1, on_date=None):
    """Return the applicable approved price or None when no approved price exists.

    Picks the most recent approved list valid on `on_date` in the requested currency and the
    largest quantity break that does not exceed `quantity`.
    """
    on_date = on_date or timezone.localdate()
    lists = (
        PriceList.objects.filter(status=PriceList.Status.APPROVED, currency=currency, valid_from__lte=on_date)
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=on_date))
        .order_by("-valid_from", "-approved_at")
    )
    for price_list in lists:
        item = (
            price_list.items.filter(variant=variant, min_quantity__lte=max(quantity, 1))
            .order_by("-min_quantity")
            .first()
        )
        if item:
            return PriceResult(unit_price=item.unit_price, item=item, price_list=price_list)
    return None


_FRACTIONS = {"½": "1/2", "¼": "1/4", "¾": "3/4"}


def normalize_size(text: str) -> str:
    """Normalise size strings: '1/2"', '1/2 inch', '½ in' -> '1/2in'; '20 mm' -> '20mm'; 'DN15' -> 'dn15'."""
    if not text:
        return ""
    s = text.strip().lower()
    for k, v in _FRACTIONS.items():
        s = s.replace(k, v)
    s = s.replace("inches", "in").replace("inch", "in").replace('"', "in").replace("''", "in").replace("”", "in")
    s = re.sub(r"\s+", "", s)
    s = s.replace("millimetres", "mm").replace("millimeters", "mm")
    s = re.sub(r"^(\d+(?:[.-]\d+)?(?:/\d+)?)$", r"\1", s)
    return s


def match_variants(*, material: str = "", category_hint: str = "", size: str = "", text: str = "", limit: int = 5):
    """Find verified, active variants that may match a customer's request (best effort, never invents)."""
    qs = ProductVariant.objects.filter(is_active=True, product__is_active=True, product__is_verified=True)
    if material:
        qs = qs.filter(product__material__iexact=material.strip())
    hint = (category_hint or text or "").lower()
    keywords = [w for w in re.findall(r"[a-z]+", hint) if len(w) > 2 and w not in {"the", "and", "for", "with", "valve", "valves"}]
    if keywords:
        cond = Q()
        for w in keywords:
            cond |= Q(product__name__icontains=w) | Q(product__category__name__icontains=w)
        narrowed = qs.filter(cond)
        if narrowed.exists():
            qs = narrowed
    target = normalize_size(size)
    if target:
        exact = [v for v in qs.select_related("product") if normalize_size(v.size) == target]
        return exact[:limit]
    return list(qs.select_related("product")[:limit])


def verified_product_catalogue() -> list[dict]:
    """Compact list of verified products for AI prompts."""
    return [
        p.summary_for_ai()
        for p in Product.objects.filter(is_active=True, is_verified=True).select_related("category").prefetch_related("variants")
    ]
