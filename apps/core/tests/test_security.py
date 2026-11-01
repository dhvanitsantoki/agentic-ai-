from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.core.models import LoginAttempt, Role
from apps.core.testing import make_user, seed_reference_data
from apps.crm.models import Company


class SecurityTests(TestCase):
    def setUp(self):
        seed_reference_data()
        self.user = make_user("owner", Role.ADMIN, password="Str0ng-Passw0rd!")

    def test_login_throttle(self):
        for _ in range(5):
            self.client.post("/accounts/login/", {"username": "owner", "password": "wrong"})
        resp = self.client.post("/accounts/login/", {"username": "owner", "password": "Str0ng-Passw0rd!"})
        self.assertEqual(resp.status_code, 429, "locked out even with the right password after 5 failures")
        self.assertEqual(LoginAttempt.objects.filter(success=False).count(), 5)

    def test_successful_login(self):
        resp = self.client.post("/accounts/login/", {"username": "owner", "password": "Str0ng-Passw0rd!"})
        self.assertEqual(resp.status_code, 302)

    def test_open_redirect_blocked(self):
        self.client.force_login(self.user)
        c = Company.objects.create(name="X")
        resp = self.client.post(f"/crm/companies/{c.pk}/stage/", {"stage": "contacted", "next": "//evil.example/phish"})
        self.assertEqual(resp["Location"], c.get_absolute_url())
        resp = self.client.post(f"/crm/companies/{c.pk}/stage/", {"stage": "replied", "next": "/crm/pipeline/"})
        self.assertEqual(resp["Location"], "/crm/pipeline/")

    def test_media_requires_login_and_admin_requires_staff(self):
        self.assertEqual(self.client.get("/media/quotations/x.pdf").status_code, 302)
        ex = make_user("exec", Role.EXECUTIVE)
        self.client.force_login(ex)
        self.assertEqual(self.client.get("/admin/").status_code, 302, "non-staff users are sent to the admin login")

    def test_upload_type_allowlist(self):
        self.client.force_login(self.user)
        bad = SimpleUploadedFile("evil.html", b"<script>alert(1)</script>", content_type="text/html")
        self.client.post("/products/documents/new/", {"title": "x", "kind": "catalogue", "file": bad})
        from apps.products.models import CompanyDocument

        self.assertFalse(CompanyDocument.objects.exists())

    def test_inbound_email_html_is_escaped(self):
        from apps.mailbox.models import InboundEmail
        from apps.outreach.tests.helpers import make_account

        e = InboundEmail.objects.create(account=make_account(), provider_message_id="x", from_email="a@b.example",
                                        subject="<script>alert(1)</script>", body_text="<img src=x onerror=alert(1)>")
        self.client.force_login(self.user)
        html = self.client.get(f"/mailbox/inbox/{e.pk}/").content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)


class WidgetFormatTests(TestCase):
    def test_html_date_inputs_render_iso_values(self):
        """<input type=date> only accepts YYYY-MM-DD; the en-GB locale would otherwise render DD/MM/YYYY."""
        import datetime

        from apps.products.forms import PriceListForm

        html = str(PriceListForm(initial={"valid_from": datetime.date(2026, 11, 1)})["valid_from"])
        self.assertIn('value="2026-11-01"', html)
