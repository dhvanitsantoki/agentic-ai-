from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from apps.agents.llm import BudgetExceeded
from apps.agents.models import AgentRun
from apps.agents.testing import FakeClient, tool_response
from apps.core.models import SystemSettings
from apps.core.testing import make_user, seed_reference_data
from apps.crm.models import Company, Contact, Stage
from apps.geo.models import Country, CountryMarketReport
from apps.research import scoring
from apps.research.models import ResearchFinding, ResearchRun
from apps.research.pipeline import execute_run
from apps.research.verification import (
    FetchResult, PageCache, _check_url, build_result, crawl_published_contacts, email_on_page, fetch, phone_on_page, text_on_page,
)

HOME = """<html><head><title>Rift Irrigation Supplies</title></head><body>
<h1>Rift Irrigation Supplies Ltd</h1><p>We distribute drip irrigation kits and PVC ball valves under our own brand AquaRift.</p>
<p>Importers of plastic valves from Asia since 2009. Dealers in 12 counties.</p>
<a href="/contact-us">Contact us</a> <a href="https://facebook.com/x">Facebook</a></body></html>"""
CONTACT = """<html><body><h2>Contact</h2><p>Purchasing Manager: Jane Wanjiru</p>
<p>Email: purchasing [at] rift-irrigation.example</p><p>Sales: <a href="mailto:sales@rift-irrigation.example">sales@rift-irrigation.example</a></p>
<p>Tel: <a href="tel:+254201234567">+254 20 123 4567</a></p><p>Designed by webagency@designer.example</p></body></html>"""
PAGES = {
    "https://rift-irrigation.example": HOME,
    "https://rift-irrigation.example/": HOME,
    "https://rift-irrigation.example/contact-us": CONTACT,
}


def fake_fetch(url):
    url = url.strip()
    if url in PAGES:
        return build_result(url, 200, url, PAGES[url])
    return FetchResult(url=url, ok=False, error="ConnectionError")


def fake_mx(domain):
    return (domain != "nomx.example", [f"mx.{domain}"])


def company_payload(**overrides):
    base = {
        "name": "Rift Irrigation Supplies Ltd", "website": "https://rift-irrigation.example", "country_code": "KE", "city": "Nairobi",
        "company_type": "distributor", "industries": ["irrigation", "agriculture"], "business_type": "both",
        "description": "Distributor of drip irrigation and PVC valves.", "product_range": "Drip kits, PVC ball valves",
        "existing_brands": "AquaRift", "potential_requirements": "PVC ball valves 20-63 mm", "estimated_size": "",
        "estimated_size_source": "", "relevance": "high", "confidence": "medium", "active_evidence": "Current catalogue",
        "signals": {k: {"present": False, "evidence": "", "source_url": ""} for k in [
            "sells_valves_own_brand", "outsources_manufacturing", "imports_plastic_valves", "manufactures_irrigation",
            "private_label_need", "distribution_network", "bulk_volume", "product_fit"]},
        "sources": [{"url": "https://rift-irrigation.example", "title": "Home", "supports": "Own brand AquaRift", "kind": "website"}],
        "contacts": [
            {"full_name": "Jane Wanjiru", "title": "Purchasing Manager", "email": "purchasing@rift-irrigation.example",
             "phone": "+254 20 123 4567", "whatsapp": "", "linkedin_url": "", "source_url": "https://rift-irrigation.example/contact-us",
             "is_decision_maker": True},
            # fabricated by the model: not on the page -> must be discarded
            {"full_name": "John Invented", "title": "CEO", "email": "john.invented@rift-irrigation.example", "phone": "+254 700 000 000",
             "whatsapp": "", "linkedin_url": "https://linkedin.com/in/fake", "source_url": "https://rift-irrigation.example/contact-us",
             "is_decision_maker": True},
        ],
    }
    base["signals"]["sells_valves_own_brand"] = {"present": True, "evidence": "PVC ball valves under our own brand AquaRift",
                                                 "source_url": "https://rift-irrigation.example"}
    base["signals"]["imports_plastic_valves"] = {"present": True, "evidence": "Importers of plastic valves from Asia since 2009",
                                                 "source_url": "https://rift-irrigation.example"}
    base["signals"]["distribution_network"] = {"present": True, "evidence": "Dealers in 12 counties",
                                               "source_url": "https://rift-irrigation.example"}
    # claimed without a reachable source -> must not score
    base["signals"]["outsources_manufacturing"] = {"present": True, "evidence": "We outsource production",
                                                   "source_url": "https://unreachable.example/page"}
    base.update(overrides)
    return base


class VerificationTests(TestCase):
    def test_email_and_phone_must_appear_on_page(self):
        page = fake_fetch("https://rift-irrigation.example/contact-us")
        self.assertTrue(email_on_page("purchasing@rift-irrigation.example", page), "obfuscated [at] address is recognised")
        self.assertTrue(email_on_page("sales@rift-irrigation.example", page))
        self.assertFalse(email_on_page("john.invented@rift-irrigation.example", page))
        self.assertTrue(phone_on_page("+254-20-1234567", page))
        self.assertFalse(phone_on_page("+254 700 000 000", page))
        self.assertTrue(text_on_page("Jane Wanjiru", page))
        self.assertFalse(text_on_page("John Invented", page))

    def test_crawl_keeps_only_company_or_freemail_addresses(self):
        cache = PageCache(fetcher=fake_fetch, mx_resolver=fake_mx)
        found = crawl_published_contacts("https://rift-irrigation.example", cache)
        self.assertIn("purchasing@rift-irrigation.example", found["emails"])
        self.assertIn("sales@rift-irrigation.example", found["emails"])
        self.assertNotIn("webagency@designer.example", found["emails"])
        self.assertEqual(found["emails"]["sales@rift-irrigation.example"], "https://rift-irrigation.example/contact-us")

    def test_ssrf_protection(self):
        for url in ("http://127.0.0.1/admin", "http://localhost:8000/", "http://10.0.0.5/", "file:///etc/passwd", "ftp://x.example/"):
            with self.assertRaises(Exception):
                _check_url(url)
            self.assertFalse(fetch(url).ok)


class ScoringTests(TestCase):
    def test_only_backed_signals_count(self):
        raw = company_payload()
        result = scoring.score_signals(raw["signals"], {"https://rift-irrigation.example"})
        present = {s["key"] for s in result["signals"] if s["present"]}
        self.assertEqual(present, {"sells_valves_own_brand", "imports_plastic_valves", "distribution_network"})
        self.assertEqual(result["score"], 25 + 15 + 10)
        self.assertEqual(scoring.categorise(result["score"], "distributor", "high", result), "B")

    def test_categories(self):
        def bd(*keys):
            return {"signals": [{"key": k, "present": True} for k in keys]}
        self.assertEqual(scoring.categorise(70, "manufacturer", "high", bd("sells_valves_own_brand", "outsources_manufacturing")), "A")
        self.assertEqual(scoring.categorise(15, "importer", "medium", bd("imports_plastic_valves")), "D")
        self.assertEqual(scoring.categorise(10, "wholesaler", "medium", bd()), "C")
        self.assertEqual(scoring.categorise(5, "other", "low", bd()), "E")
        self.assertEqual(scoring.categorise(90, "other", "none", bd("sells_valves_own_brand")), "E")


@override_settings(ANTHROPIC_API_KEY="test-key")
class ResearchPipelineTests(TestCase):
    def setUp(self):
        seed_reference_data()
        self.user = make_user()
        self.kenya = Country.objects.get(iso2="KE")
        self.existing = Company.objects.create(name="Existing Agro", website="https://existing-agro.example", country=self.kenya)

    def _run(self, payload_companies, kind=ResearchRun.Kind.BUYER, seen=()):
        run = ResearchRun.objects.create(kind=kind, target_count=5, triggered_by=self.user)
        run.countries.set([self.kenya])
        client = FakeClient([tool_response("submit_companies", {"companies": payload_companies, "search_notes": "notes"}, seen_urls=seen)])
        with mock.patch("apps.agents.llm.get_client", return_value=client):
            execute_run(run, cache=PageCache(fetcher=fake_fetch, mx_resolver=fake_mx))
        run.refresh_from_db()
        return run, client

    def test_end_to_end_only_verified_data_reaches_crm(self):
        unreachable = company_payload(name="Ghost Valves", website="https://ghost-valves.example", contacts=[], sources=[])
        duplicate = company_payload(name="Existing Agro Ltd", website="https://www.existing-agro.example/en", contacts=[], sources=[])
        run, client = self._run([company_payload(), unreachable, duplicate], seen=["https://rift-irrigation.example"])

        self.assertEqual(run.status, ResearchRun.Status.COMPLETED)
        self.assertEqual(run.added_count, 1)
        self.assertEqual(run.review_count, 1)
        self.assertEqual(run.duplicate_count, 1)
        company = Company.objects.get(name="Rift Irrigation Supplies Ltd")
        emails = set(company.contacts.values_list("email", flat=True))
        self.assertEqual(emails, {"purchasing@rift-irrigation.example", "sales@rift-irrigation.example"})
        self.assertFalse(Contact.objects.filter(email="john.invented@rift-irrigation.example").exists())
        self.assertFalse(Contact.objects.filter(full_name="John Invented").exists())
        jane = company.contacts.get(email="purchasing@rift-irrigation.example")
        self.assertEqual(jane.full_name, "Jane Wanjiru")
        self.assertEqual(jane.email_status, Contact.EmailStatus.VERIFIED)
        self.assertTrue(jane.is_primary)
        self.assertEqual(jane.phone, "+254 20 123 4567")
        self.assertEqual(company.stage, Stage.VERIFIED)
        self.assertEqual(company.oem_score, 50)
        self.assertEqual(company.oem_category, "B")
        finding = run.findings.get(company=company)
        rejected = {(r["field"], r["value"]) for r in finding.verification["rejected"]}
        self.assertIn(("email", "john.invented@rift-irrigation.example"), rejected)
        self.assertIn(("phone", "+254 700 000 000"), rejected)
        self.assertIn(("full_name", "John Invented"), rejected)
        self.assertIn(("linkedin_url", "https://linkedin.com/in/fake"), rejected)
        ghost = run.findings.get(name="Ghost Valves")
        self.assertEqual(ghost.status, ResearchFinding.Status.NEEDS_REVIEW)
        self.assertFalse(Company.objects.filter(name="Ghost Valves").exists())
        self.assertEqual(run.findings.get(name="Existing Agro Ltd").company, self.existing)
        # cost metering and request shape
        agent_run = AgentRun.objects.get(object_id=str(run.pk))
        self.assertGreater(agent_run.cost_usd, Decimal("0"))
        self.assertEqual(agent_run.web_searches, 2)
        call = client.messages.calls[0]
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual(call["fallbacks"], "default")
        tool_types = [t.get("type") for t in call["tools"]]
        self.assertIn("web_search_20260209", tool_types)
        self.assertIn("web_fetch_20260209", tool_types)
        self.assertTrue(call["tools"][-1]["strict"])
        # the exclusion list of known domains is sent in the prompt
        self.assertIn("existing-agro.example", call["messages"][0]["content"])

    def test_daily_limit_skips_research(self):
        sys = SystemSettings.get_solo()
        sys.research_daily_company_limit = 0
        sys.save()
        run = ResearchRun.objects.create(kind=ResearchRun.Kind.BUYER, target_count=5)
        run.countries.set([self.kenya])
        with mock.patch("apps.agents.llm.get_client", return_value=FakeClient([])):
            execute_run(run, cache=PageCache(fetcher=fake_fetch, mx_resolver=fake_mx))
        run.refresh_from_db()
        self.assertEqual(run.status, ResearchRun.Status.COMPLETED)
        self.assertIn("daily", run.summary)
        self.assertEqual(AgentRun.objects.get(object_id=str(run.pk)).status, AgentRun.Status.SKIPPED)

    def test_budget_cap_stops_ai_calls(self):
        sys = SystemSettings.get_solo()
        sys.monthly_ai_budget_usd = Decimal("1.00")
        sys.save()
        AgentRun.objects.create(agent="buyer_research", action="x", status="succeeded", cost_usd=Decimal("1.50"))
        run = ResearchRun.objects.create(kind=ResearchRun.Kind.BUYER, target_count=5)
        run.countries.set([self.kenya])
        client = FakeClient([])
        with mock.patch("apps.agents.llm.get_client", return_value=client), self.assertRaises(BudgetExceeded):
            execute_run(run, cache=PageCache(fetcher=fake_fetch, mx_resolver=fake_mx))
        run.refresh_from_db()
        self.assertEqual(run.status, ResearchRun.Status.FAILED)
        self.assertEqual(client.messages.calls, [])

    def test_country_report_marks_unseen_sources_and_low_confidence(self):
        run = ResearchRun.objects.create(kind=ResearchRun.Kind.COUNTRY, target_count=1)
        run.countries.set([self.kenya])
        data = {"summary": "s", "demand_level": "medium", "demand_assessment": "d", "buyer_types": "b", "import_requirements": "i",
                "relevant_standards": "KS", "competitors": "c", "opportunities": "o", "risks": "r", "confidence": "high",
                "sources": [{"url": "https://stats.example/a", "title": "A", "supports": "x"},
                            {"url": "https://made-up.example/b", "title": "B", "supports": "y"}]}
        client = FakeClient([tool_response("submit_market_report", data, seen_urls=["https://stats.example/a"])])
        with mock.patch("apps.agents.llm.get_client", return_value=client):
            execute_run(run)
        report = CountryMarketReport.objects.get(research_run=run)
        seen = {s["url"]: s["seen_by_search"] for s in report.sources}
        self.assertTrue(seen["https://stats.example/a"])
        self.assertFalse(seen["https://made-up.example/b"])
        self.assertEqual(report.status, CountryMarketReport.Status.DRAFT)
        self.assertTrue(report.generated_by_ai)

    def test_deep_dive_updates_company(self):
        company = Company.objects.create(name="Rift Irrigation Supplies Ltd", website="https://rift-irrigation.example", country=self.kenya)
        run = ResearchRun.objects.create(kind=ResearchRun.Kind.DEEP_DIVE, company=company, target_count=1)
        client = FakeClient([tool_response("submit_company_profile", {"company": company_payload(), "search_notes": ""})])
        with mock.patch("apps.agents.llm.get_client", return_value=client):
            execute_run(run, cache=PageCache(fetcher=fake_fetch, mx_resolver=fake_mx))
        company.refresh_from_db()
        self.assertEqual(company.existing_brands, "AquaRift")
        self.assertEqual(company.oem_category, "B")
        self.assertTrue(company.contacts.filter(email="purchasing@rift-irrigation.example", email_status="verified").exists())
