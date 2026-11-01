from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.core.testing import make_user
from apps.products.models import PriceList, PriceListItem, Product, ProductCategory, ProductVariant
from apps.products.seeds import CATEGORIES, seed_categories
from apps.products.services import get_approved_price, match_variants, normalize_size


class ProductDatabaseTests(TestCase):
    def setUp(self):
        seed_categories()
        self.user = make_user()
        self.cat = ProductCategory.objects.get(name="PVC Ball Valves")
        self.product = Product.objects.create(category=self.cat, name="Test PVC ball valve", material="PVC",
                                              pressure_rating="PN10", connection_types="Solvent socket")
        self.v20 = ProductVariant.objects.create(product=self.product, sku="T-PVC-20", size='1/2"', size_mm=Decimal("20"))
        self.v25 = ProductVariant.objects.create(product=self.product, sku="T-PVC-25", size='3/4"', size_mm=Decimal("25"))

    def test_seeded_categories_match_brief(self):
        self.assertEqual(ProductCategory.objects.count(), len(CATEGORIES))
        seed_categories()  # idempotent
        self.assertEqual(ProductCategory.objects.count(), len(CATEGORIES))

    def _approved_list(self, currency="USD", **kw):
        pl = PriceList.objects.create(name=f"List {currency}", currency=currency, valid_from=timezone.localdate() - timedelta(days=1), **kw)
        PriceListItem.objects.create(price_list=pl, variant=self.v20, unit_price=Decimal("1.10"), min_quantity=1)
        PriceListItem.objects.create(price_list=pl, variant=self.v20, unit_price=Decimal("0.95"), min_quantity=1000)
        pl.approve(self.user)
        return pl

    def test_price_only_from_approved_list(self):
        draft = PriceList.objects.create(name="Draft", currency="USD")
        PriceListItem.objects.create(price_list=draft, variant=self.v20, unit_price=Decimal("0.50"))
        self.assertIsNone(get_approved_price(self.v20, "USD", 10))
        self._approved_list()
        self.assertEqual(get_approved_price(self.v20, "USD", 10).unit_price, Decimal("1.10"))

    def test_quantity_breaks_and_currency(self):
        self._approved_list()
        self.assertEqual(get_approved_price(self.v20, "USD", 1500).unit_price, Decimal("0.95"))
        self.assertIsNone(get_approved_price(self.v20, "EUR", 10), "No EUR list - must not convert or invent")
        self.assertIsNone(get_approved_price(self.v25, "USD", 10), "Size without a price returns None")

    def test_expired_list_not_used(self):
        pl = self._approved_list()
        pl.valid_until = timezone.localdate() - timedelta(days=1)
        PriceList.objects.filter(pk=pl.pk).update(valid_until=pl.valid_until)
        self.assertIsNone(get_approved_price(self.v20, "USD", 10))

    def test_approved_list_is_locked(self):
        pl = self._approved_list()
        item = pl.items.first()
        item.unit_price = Decimal("0.01")
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(ValidationError):
            item.delete()
        with self.assertRaises(ValidationError):
            PriceListItem.objects.create(price_list=pl, variant=self.v25, unit_price=Decimal("2"))

    def test_empty_list_cannot_be_approved(self):
        pl = PriceList.objects.create(name="Empty", currency="USD")
        with self.assertRaises(ValidationError):
            pl.approve(self.user)

    def test_size_normalisation(self):
        self.assertEqual(normalize_size('1/2"'), "1/2in")
        self.assertEqual(normalize_size("1/2 inch"), "1/2in")
        self.assertEqual(normalize_size("½ in"), "1/2in")
        self.assertEqual(normalize_size("20 mm"), "20mm")

    def test_matching_uses_only_verified_products(self):
        self.assertEqual(match_variants(material="PVC", size='1/2 inch'), [])
        self.product.mark_verified(self.user)
        self.assertEqual(match_variants(material="PVC", size='1/2 inch'), [self.v20])
        self.assertEqual(match_variants(material="CPVC", size='1/2 inch'), [])

    def test_missing_fields_reported(self):
        missing = self.product.missing_fields()
        self.assertIn("Technical specifications", missing)
        self.assertIn("Catalogue PDF", missing)
        self.assertNotIn("Pressure rating", missing)


class InlineFormTests(TestCase):
    """Post exactly the fields the inline templates render (regression: unchecked/missing fields)."""

    def setUp(self):
        seed_categories()
        self.user = make_user()
        self.client.force_login(self.user)
        self.product = Product.objects.create(category=ProductCategory.objects.first(), name="Inline valve", material="PP")

    def test_added_size_is_active(self):
        self.client.post(f"/products/{self.product.pk}/variants/add/", {"sku": "IN-1", "size": "25 mm", "size_mm": "", "pressure_rating": "",
                                                                       "connection_type": "", "moq": "", "packaging": ""})
        v = ProductVariant.objects.get(sku="IN-1")
        self.assertTrue(v.is_active)

    def test_image_upload_without_sort_order(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        img = SimpleUploadedFile("v.png", b"\\x89PNG\\r\\n\\x1a\\n" + b"0" * 20, content_type="image/png")
        self.client.post(f"/products/{self.product.pk}/images/add/", {"image": img, "caption": "front"})
        self.assertEqual(self.product.images.count(), 1)

    def test_task_quick_add(self):
        from apps.crm.models import Task

        self.client.post("/crm/tasks/new/", {"title": "Call", "company": "", "assigned_to": self.user.pk, "priority": "normal", "due_date": ""})
        self.assertEqual(Task.objects.get().kind, "other")
