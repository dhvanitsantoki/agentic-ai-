from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.core.models import CompanyProfile, Role
from apps.core.testing import make_user, seed_reference_data
from apps.crm.models import Requirement, Stage
from apps.outreach.models import EmailKind, OutboundEmail
from apps.outreach.seeds import seed_templates
from apps.outreach.sending import compose
from apps.outreach.tests.helpers import configure, make_account, make_company
from apps.products.models import PriceList, PriceListItem, Product, ProductCategory, ProductVariant
from apps.quotations import services
from apps.quotations.models import Quotation


class QuotationTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_templates()
        configure()
        self.manager = make_user("mgr", Role.MANAGER)
        self.exec = make_user("exec", Role.EXECUTIVE)
        self.company, self.contact = make_company()
        cat = ProductCategory.objects.get(name="PVC Ball Valves")
        self.product = Product.objects.create(category=cat, name="PVC Ball Valve", material="PVC", pressure_rating="PN10",
                                              connection_types="Solvent socket", default_moq=1000)
        self.product.mark_verified(self.manager)
        self.v = ProductVariant.objects.create(product=self.product, sku="PVC-15", size='1/2"', packaging="100 pcs/carton")
        pl = PriceList.objects.create(name="Export 2026", currency="USD", valid_from=timezone.localdate() - timedelta(days=1))
        PriceListItem.objects.create(price_list=pl, variant=self.v, unit_price=Decimal("0.8500"), min_quantity=1)
        PriceListItem.objects.create(price_list=pl, variant=self.v, unit_price=Decimal("0.7900"), min_quantity=5000)
        pl.approve(self.manager)
        self.req = Requirement.objects.create(company=self.company, contact=self.contact, description="PVC ball valve",
                                              material="PVC", sizes="1/2 inch", quantity="5,000 pcs")

    def _complete(self, q):
        q.incoterm, q.port_of_loading, q.payment_terms, q.lead_time = "FOB", "Mundra", "As agreed", "To be confirmed"
        q.valid_until = timezone.localdate() + timedelta(days=30)
        q.freight_note = "Not included (FOB)"
        q.save()
        services.refresh_status(q)
        return q

    def test_prices_come_from_approved_list_with_quantity_break(self):
        q = services.create_quotation(self.company, requirements=[self.req], user=self.exec)
        line = q.lines.get()
        self.assertEqual(line.variant, self.v)
        self.assertEqual(line.quantity, 5000)
        self.assertEqual(line.unit_price, Decimal("0.7900"))
        self.assertEqual(line.moq, 1000)
        self.assertEqual(line.packaging, "100 pcs/carton")
        self.assertEqual(q.subtotal, Decimal("3950.00"))
        self.assertEqual(q.status, Quotation.Status.NEEDS_INPUT, "commercial terms missing")
        self.assertIn("Payment terms", q.missing_fields)

    def test_company_defaults_fill_terms_only_when_configured(self):
        p = CompanyProfile.get_solo()
        p.default_payment_terms, p.default_incoterm, p.default_port_of_loading = "30% advance, 70% against B/L", "FOB", "Mundra"
        p.default_lead_time, p.quotation_validity_days = "30 days from advance", 30
        p.save()
        q = services.create_quotation(self.company, requirements=[self.req])
        self.assertEqual(q.payment_terms, "30% advance, 70% against B/L")
        self.assertEqual(q.missing_fields, ["Freight (amount or note)"])

    def test_other_currency_has_no_price_and_needs_human(self):
        q = services.create_quotation(self.company, currency="EUR", requirements=[self.req])
        self.assertIsNone(q.lines.get().unit_price)
        self.assertTrue(any("approved price" in m for m in q.missing_fields))

    def test_unmatched_or_unverified_product_needs_human(self):
        self.product.is_verified = False
        self.product.save()
        q = services.create_quotation(self.company, requirements=[self.req])
        self.assertIsNone(q.lines.get().variant)
        self.assertTrue(any("verified product" in m for m in q.missing_fields))

    def test_approval_rules_pdf_and_lock(self):
        q = self._complete(services.create_quotation(self.company, requirements=[self.req]))
        self.assertEqual(q.status, Quotation.Status.DRAFT)
        services.submit_for_approval(q, self.exec)
        with self.assertRaises(PermissionDenied):
            services.approve(q, self.exec)
        with self.assertRaises(PermissionDenied):
            services.set_discount(q, Decimal("5"), self.exec)
        services.set_discount(q, Decimal("5"), self.manager)
        services.approve(q, self.manager)
        q.refresh_from_db()
        self.assertEqual(q.status, Quotation.Status.APPROVED)
        self.assertTrue(q.pdf.name.endswith(".pdf"))
        q.pdf.open("rb")
        self.assertTrue(q.pdf.read(4) == b"%PDF")
        q.pdf.close()
        self.assertEqual(q.total, Decimal("3950.00") - Decimal("197.50"))
        with self.assertRaises(ValidationError):
            services.set_discount(q, Decimal("10"), self.manager)
        line = q.lines.get()
        with self.assertRaises(ValidationError):
            services.override_price(line, Decimal("0.10"), self.manager)

    def test_cannot_approve_with_missing_data(self):
        q = services.create_quotation(self.company, requirements=[self.req])
        with self.assertRaises(ValidationError):
            services.approve(q, self.manager)

    def test_manual_price_requires_manager(self):
        q = services.create_quotation(self.company, currency="EUR", requirements=[self.req])
        line = q.lines.get()
        with self.assertRaises(PermissionDenied):
            services.override_price(line, Decimal("0.75"), self.exec)
        services.override_price(line, Decimal("0.75"), self.manager)
        line.refresh_from_db()
        self.assertEqual(line.price_overridden_by, self.manager)

    def test_send_creates_email_with_pdf_and_stage_quoted(self):
        q = self._complete(services.create_quotation(self.company, requirements=[self.req]))
        services.approve(q, self.manager)
        email = services.send_quotation(q, self.manager)
        self.assertEqual(email.kind, EmailKind.QUOTATION)
        self.assertEqual(email.status, OutboundEmail.Status.PENDING_APPROVAL)
        msg = compose(email, make_account())
        self.assertTrue(any(a.filename.endswith(".pdf") and a.content.startswith(b"%PDF") for a in msg.attachments))
        from apps.outreach.sending import after_sent

        email.status, email.sent_at = "sent", timezone.now()
        email.save()
        after_sent(email)
        q.refresh_from_db()
        self.company.refresh_from_db()
        self.assertEqual(q.status, Quotation.Status.SENT)
        self.assertEqual(self.company.stage, Stage.QUOTED)

    def test_revision_and_expiry(self):
        q = self._complete(services.create_quotation(self.company, requirements=[self.req]))
        services.approve(q, self.manager)
        rev = services.new_revision(q, self.manager)
        self.assertTrue(rev.number.endswith("-R1"))
        self.assertEqual(rev.lines.count(), 1)
        self.assertFalse(rev.is_locked)
        Quotation.objects.filter(pk=q.pk).update(valid_until=timezone.localdate() - timedelta(days=1))
        self.assertEqual(services.expire_quotations(), 1)

    def test_below_moq_is_flagged(self):
        self.req.quantity = "200"
        self.req.save()
        q = services.create_quotation(self.company, requirements=[self.req])
        self.assertTrue(any("below MOQ" in m for m in q.missing_fields))

    def test_views_enforce_roles(self):
        q = self._complete(services.create_quotation(self.company, requirements=[self.req]))
        self.client.force_login(self.exec)
        self.client.post(f"/quotations/{q.pk}/action/", {"action": "approve"})
        q.refresh_from_db()
        self.assertNotEqual(q.status, Quotation.Status.APPROVED)
        self.client.force_login(self.manager)
        self.client.post(f"/quotations/{q.pk}/action/", {"action": "approve"})
        q.refresh_from_db()
        self.assertEqual(q.status, Quotation.Status.APPROVED)
        self.assertEqual(self.client.get(f"/quotations/{q.pk}/pdf/").status_code, 200)
