"""Render every main page as each role to catch template/URL errors."""
from django.test import TestCase
from django.urls import reverse

from apps.core.management.commands.setup_initial_data import Command as Setup
from apps.core.models import Role
from apps.core.testing import make_user
from apps.crm.models import Company, Contact
from apps.geo.models import Country
from apps.products.models import PriceList, Product, ProductCategory, ProductVariant
from apps.quotations.services import create_quotation
from apps.reports.services import generate_report

PAGES = [
    "dashboard", "crm:pipeline", "crm:company_list", "crm:company_create", "crm:company_import", "crm:contact_list",
    "crm:task_list", "research:run_list", "research:run_create", "research:review_queue", "geo:market_overview",
    "outreach:approval_queue", "outreach:email_list", "outreach:template_list", "outreach:suppression_list",
    "mailbox:inbox", "mailbox:account_list", "followups:enrollment_list", "followups:sequence_create",
    "quotations:list", "products:product_list", "products:product_create", "products:category_list",
    "products:document_list", "products:document_create", "products:pricelist_list", "products:pricelist_create",
    "reports:list", "agents:control", "agents:job_list", "agents:run_log", "core:settings_home", "core:company_profile",
    "core:system_settings", "core:user_list", "core:user_create", "core:audit_log", "core:notifications", "password_change",
    "two_factor_setup", "products:product_import", "products:sizes_import",
]


class SmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Setup().handle()
        cls.admin = make_user("admin1", Role.ADMIN)
        cls.viewer = make_user("viewer1", Role.VIEWER)
        cat = ProductCategory.objects.first()
        p = Product.objects.create(category=cat, name="Smoke valve", material="PVC")
        ProductVariant.objects.create(product=p, sku="SMK-1", size="20 mm")
        PriceList.objects.create(name="Smoke list", currency="USD")
        cls.company = Company.objects.create(name="Smoke Co", website="https://smoke.example", country=Country.objects.get(iso2="KE"))
        Contact.objects.create(company=cls.company, email="buyer@smoke.example", full_name="A Buyer")
        cls.quote = create_quotation(cls.company, user=cls.admin)
        cls.report = generate_report("daily", narrative=False, email=False)

    def test_all_pages_render_for_admin(self):
        self.client.force_login(self.admin)
        for name in PAGES:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, f"{name} -> {resp.status_code}")
        detail_urls = [
            self.company.get_absolute_url(), reverse("crm:company_edit", args=[self.company.pk]),
            reverse("crm:contact_create", args=[self.company.pk]), reverse("crm:requirement_create", args=[self.company.pk]),
            reverse("quotations:detail", args=[self.quote.pk]), reverse("quotations:pdf", args=[self.quote.pk]),
            reverse("quotations:create_for_company", args=[self.company.pk]),
            reverse("geo:country_detail", args=["KE"]), reverse("products:product_detail", args=[Product.objects.first().pk]),
            reverse("products:pricelist_detail", args=[PriceList.objects.first().pk]),
            reverse("reports:detail", args=[self.report.pk]), reverse("reports:export", args=[self.report.pk, "xlsx"]),
            reverse("reports:export", args=[self.report.pk, "pdf"]), reverse("crm:company_list") + "?export=xlsx",
            reverse("outreach:compose") + f"?company={self.company.pk}", reverse("crm:contact_export"),
        ]
        for url in detail_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, f"{url} -> {resp.status_code}")

    def test_viewer_can_read_but_not_write(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(self.company.get_absolute_url()).status_code, 200)
        resp = self.client.post(reverse("crm:company_stage", args=[self.company.pk]), {"stage": "contacted"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.get(reverse("core:system_settings")).status_code, 403)

    def test_anonymous_redirected_and_health_public(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        self.assertEqual(self.client.get("/healthz").status_code, 200)
