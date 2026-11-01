from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.core.testing import make_user, seed_reference_data
from apps.crm import services
from apps.crm.models import Company, Contact, Stage, StageHistory, normalize_name
from apps.geo.models import Country


class CrmServiceTests(TestCase):
    def setUp(self):
        seed_reference_data()
        self.user = make_user()
        self.kenya = Country.objects.get(iso2="KE")
        self.company = Company.objects.create(name="Acme Irrigation Ltd", website="https://www.acme-irrigation.example/products",
                                              country=self.kenya)

    def test_countries_seeded_by_continent_with_timezones(self):
        self.assertGreater(Country.objects.count(), 200)
        self.assertEqual(Country.objects.get(iso2="IN").continent_id, "AS")
        self.assertEqual(Country.objects.get(iso2="BR").continent_id, "SA")
        self.assertEqual(Country.objects.get(iso2="MX").continent_id, "NA")
        self.assertEqual(Country.objects.get(iso2="AU").timezone, "Australia/Sydney")
        self.assertEqual(Country.objects.get(iso2="KE").continent_id, "AF")
        self.assertEqual(Country.objects.get(iso2="DE").continent_id, "EU")
        self.assertEqual(Country.objects.get(iso2="NZ").continent_id, "OC")

    def test_domain_and_name_normalisation(self):
        self.assertEqual(self.company.domain, "acme-irrigation.example")
        self.assertEqual(normalize_name("ACME Irrigation Co., Ltd."), "acme irrigation")

    def test_duplicate_detection(self):
        self.assertEqual(services.find_duplicate("Other name", "http://acme-irrigation.example"), self.company)
        self.assertEqual(services.find_duplicate("Acme Irrigation Limited", "", "KE"), self.company)
        self.assertIsNone(services.find_duplicate("Acme Irrigation Limited", "", "TZ"))
        Contact.objects.create(company=self.company, email="buyer@acme-irrigation.example")
        self.assertEqual(services.find_duplicate("x", "", "", ["BUYER@acme-irrigation.example"]), self.company)
        self.assertIsNone(services.find_duplicate("x", "", "", ["someone@gmail.com"]), "free-mail domains never match")

    def test_advance_stage_only_moves_forward(self):
        self.assertTrue(services.advance_stage(self.company, Stage.CONTACTED, agent="outreach"))
        self.assertFalse(services.advance_stage(self.company, Stage.VERIFIED, agent="crm"))
        self.company.refresh_from_db()
        self.assertEqual(self.company.stage, Stage.CONTACTED)
        self.assertEqual(StageHistory.objects.filter(company=self.company).count(), 1)
        self.assertTrue(self.company.activities.filter(kind="stage").exists())

    def test_lost_company_reopens_only_on_engagement(self):
        services.set_stage(self.company, Stage.LOST, user=self.user, reason="declined")
        self.assertFalse(services.advance_stage(self.company, Stage.QUOTED, agent="x"))
        self.assertTrue(services.advance_stage(self.company, Stage.INTERESTED, agent="mailbox"))

    def test_match_sender(self):
        c = Contact.objects.create(company=self.company, email="p.manager@acme-irrigation.example")
        self.assertEqual(services.match_sender("P.Manager@acme-irrigation.example"), (self.company, c))
        self.assertEqual(services.match_sender("new.person@acme-irrigation.example"), (self.company, None))
        self.assertEqual(services.match_sender("someone@gmail.com"), (None, None))

    def test_escalation_creates_single_task_and_notification(self):
        t1 = services.escalate(self.company, "Hot lead: Acme wants a quotation", "details")
        t2 = services.escalate(self.company, "Hot lead: Acme wants a quotation", "details")
        self.assertEqual(t1, t2)
        from apps.core.models import Notification

        self.assertEqual(Notification.objects.count(), 1)


class CompanyImportTests(TestCase):
    def setUp(self):
        seed_reference_data()
        self.user = make_user()
        self.client.force_login(self.user)

    def test_csv_import_creates_dedupes_and_respects_confirmation(self):
        csv = (
            "name,website,country,city,company_type,contact_name,contact_title,email,phone,source_url,notes\n"
            "Delta Agro,deltaagro.example,Kenya,Nairobi,distributor,J Doe,Purchasing,j.doe@deltaagro.example,,https://expo.example/list,Expo 2026\n"
            "Delta Agro Ltd,,KE,,,,,,,,\n"
            "Nowhere Co,,Atlantis,,,,,,,,\n"
        ).encode()
        f = SimpleUploadedFile("leads.csv", csv, content_type="text/csv")
        resp = self.client.post("/crm/companies/import/", {"file": f, "lead_source": "exhibition", "emails_confirmed": "on"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Company.objects.count(), 2)
        delta = Company.objects.get(name="Delta Agro")
        self.assertEqual(delta.domain, "deltaagro.example")
        self.assertEqual(delta.country_id, "KE")
        contact = delta.contacts.get()
        self.assertEqual(contact.email_status, Contact.EmailStatus.MANUAL)
        self.assertTrue(contact.is_emailable)
        self.assertTrue(delta.sources.filter(url="https://expo.example/list").exists())
