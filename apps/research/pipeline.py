"""Research pipeline: AI discovery -> deterministic verification -> de-duplication -> scoring -> CRM.

Only data that survives verification reaches the CRM:
* e-mails/phones must appear on their cited page (otherwise dropped and logged on the finding);
* named people must appear on their cited page;
* buying signals count only if their source page was reachable.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.agents.llm import agent_run, research_call
from apps.agents.models import AgentName
from apps.agents.registry import ensure_not_stopped
from apps.core.audit import audit
from apps.core.models import SystemSettings
from apps.crm import services as crm
from apps.crm.models import (
    BusinessType, Company, CompanySource, CompanyType, Contact, Industry, LeadSource, Stage, domain_from_url,
)
from apps.geo.models import Country, CountryMarketReport

from . import prompts, scoring
from .models import ResearchFinding, ResearchRun
from .verification import PageCache, check_website, crawl_published_contacts, phone_on_page, text_on_page, verify_email

log = logging.getLogger(__name__)
VALID_TYPES = {c for c, _ in CompanyType.choices}
VALID_INDUSTRIES = {c for c, _ in Industry.choices}


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def companies_added_today() -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return Company.objects.filter(created_at__gte=start, lead_source__in=[LeadSource.AI_RESEARCH, LeadSource.AI_OEM]).count()


def verify_company_payload(raw: dict, cache: PageCache, seen_urls: set[str]) -> dict:
    """Run all deterministic checks on one AI-reported company. Returns a verification record."""
    website = _norm_url(raw.get("website"))
    v = {"website": check_website(website, cache) if website else {"ok": False, "error": "no website"}}
    reachable = set()
    sources = []
    for s in raw.get("sources") or []:
        url = _norm_url(s.get("url"))
        if not url:
            continue
        page = cache.get(url)
        if page.ok:
            reachable.add(url)
        sources.append({**s, "url": url, "reachable": page.ok, "seen_by_search": url in seen_urls})
    if v["website"].get("ok"):
        reachable.add(website)
    v["sources"] = sources

    contacts, rejected = [], []
    for c in raw.get("contacts") or []:
        src = _norm_url(c.get("source_url"))
        page = cache.get(src) if src else None
        if page is not None and page.ok:
            reachable.add(src)
        kept = {"title": "", "full_name": "", "email": "", "phone": "", "whatsapp": "", "linkedin_url": "",
                "source_url": src, "is_decision_maker": bool(c.get("is_decision_maker")), "email_checks": {}}
        if c.get("email"):
            checks = verify_email(c["email"], src, cache)
            if checks["result"] in ("verified", "invalid") and checks.get("found_on_source"):
                kept["email"] = checks["email"]
                kept["email_checks"] = checks
            else:
                rejected.append({"field": "email", "value": c["email"], "source_url": src, "reason": checks["result"]})
        for field in ("phone", "whatsapp"):
            if c.get(field):
                if page is not None and phone_on_page(c[field], page):
                    kept[field] = c[field].strip()
                else:
                    rejected.append({"field": field, "value": c[field], "source_url": src, "reason": "not found on source page"})
        if c.get("full_name"):
            if page is not None and text_on_page(c["full_name"], page):
                kept["full_name"] = c["full_name"].strip()
                kept["title"] = (c.get("title") or "").strip()
            else:
                rejected.append({"field": "full_name", "value": c["full_name"], "source_url": src, "reason": "name not on source page"})
                kept["is_decision_maker"] = False
        li = (c.get("linkedin_url") or "").strip()
        if li and "linkedin.com/" in li and (li in seen_urls or (page is not None and li in (page.html or ""))):
            kept["linkedin_url"] = li
        elif li:
            rejected.append({"field": "linkedin_url", "value": li, "reason": "not seen in search results or source page"})
        if kept["email"] or kept["phone"] or kept["full_name"]:
            contacts.append(kept)
    if v["website"].get("ok"):
        published = crawl_published_contacts(website, cache)
        v["published_pages"] = published["pages"]
        known = {c["email"] for c in contacts if c["email"]}
        for email, src in published["emails"].items():
            if email in known:
                continue
            checks = verify_email(email, src, cache)
            if checks["result"] in ("verified", "invalid"):
                contacts.append({"full_name": "", "title": "", "email": email, "phone": "", "whatsapp": "", "linkedin_url": "",
                                 "source_url": src, "is_decision_maker": False, "email_checks": checks})
                known.add(email)
        v["published_phones"] = published["phones"]
    v["contacts"] = contacts
    v["rejected"] = rejected
    v["reachable_urls"] = sorted(reachable)

    def quote_checker(quote, url):
        return text_on_page(quote, cache.get(url))

    v["score"] = scoring.score_signals(raw.get("signals") or {}, reachable, quote_checker)
    return v


def _apply_contacts(company: Company, contacts: list[dict]) -> tuple[int, int]:
    verified = unverified = 0
    for c in contacts:
        email = c.get("email") or ""
        existing = company.contacts.filter(email__iexact=email).first() if email else None
        if existing is None and c.get("full_name"):
            existing = company.contacts.filter(full_name__iexact=c["full_name"]).first()
        contact = existing or Contact(company=company)
        for f in ("full_name", "title", "phone", "whatsapp", "linkedin_url"):
            if c.get(f) and not getattr(contact, f):
                setattr(contact, f, c[f])
        if email and not contact.email:
            contact.email = email
        if email and contact.email == email:
            result = (c.get("email_checks") or {}).get("result")
            if contact.email_status not in (Contact.EmailStatus.MANUAL, Contact.EmailStatus.BOUNCED):
                contact.email_status = Contact.EmailStatus.VERIFIED if result == "verified" else Contact.EmailStatus.INVALID
            contact.email_checks = c.get("email_checks") or {}
            contact.email_source_url = c.get("source_url", "")
        if c.get("phone") or c.get("whatsapp"):
            contact.phone_source_url = contact.phone_source_url or c.get("source_url", "")
        contact.is_decision_maker = contact.is_decision_maker or c.get("is_decision_maker", False)
        contact.save()
        if contact.email_status == Contact.EmailStatus.VERIFIED:
            verified += 1
        elif contact.email:
            unverified += 1
    if company.contacts.exists() and not company.contacts.filter(is_primary=True).exists():
        best = company.contacts.filter(is_decision_maker=True, email_status=Contact.EmailStatus.VERIFIED).first() \
            or company.contacts.filter(email_status=Contact.EmailStatus.VERIFIED).first()
        if best:
            best.is_primary = True
            best.save(update_fields=["is_primary"])
    return verified, unverified


def _apply_sources(company: Company, sources: list[dict]):
    for s in sources:
        CompanySource.objects.update_or_create(
            company=company, url=s["url"][:1000],
            defaults={"title": (s.get("title") or "")[:300], "supports": s.get("supports") or "",
                      "kind": s.get("kind") if s.get("kind") in dict(CompanySource._meta.get_field("kind").choices) else "other",
                      "verified_reachable": s.get("reachable")},
        )


def _apply_profile(company: Company, raw: dict, v: dict, *, overwrite: bool):
    def put(field, value):
        if value and (overwrite or not getattr(company, field)):
            setattr(company, field, value)

    put("description", raw.get("description"))
    put("product_range", raw.get("product_range"))
    put("existing_brands", (raw.get("existing_brands") or "")[:500])
    put("potential_requirements", raw.get("potential_requirements"))
    put("city", (raw.get("city") or "")[:120])
    if raw.get("estimated_size") and raw.get("estimated_size_source") in v["reachable_urls"]:
        company.estimated_size = raw["estimated_size"][:120]
        company.estimated_size_source = raw["estimated_size_source"][:500]
    ctype = raw.get("company_type")
    if ctype in VALID_TYPES and (overwrite or company.company_type == CompanyType.OTHER):
        company.company_type = ctype
    inds = [i for i in raw.get("industries") or [] if i in VALID_INDUSTRIES]
    if inds:
        company.industries = sorted(set(company.industries or []) | set(inds))
    btype = raw.get("business_type")
    if btype in dict(BusinessType.choices) and (overwrite or company.business_type == BusinessType.UNKNOWN):
        company.business_type = btype
    score = v["score"]
    if overwrite or score["score"] >= company.oem_score:
        company.oem_score = score["score"]
        company.score_breakdown = score
        company.oem_category = scoring.categorise(score["score"], company.company_type, raw.get("relevance", "medium"), score)
        company.research_confidence = raw.get("confidence", "")
        evidence = [f"{s['label']}: \"{s['evidence']}\" ({s['source_url']})" for s in score["signals"] if s["present"]]
        company.fit_evidence = "\n".join(evidence)
    company.verification_status = (Company.VerificationStatus.VERIFIED if v["website"].get("ok")
                                   else Company.VerificationStatus.INACTIVE)
    company.website_checked_at = timezone.now()


@transaction.atomic
def ingest_company(run: ResearchRun, raw: dict, v: dict, *, lead_source: str) -> ResearchFinding:
    name = (raw.get("name") or "").strip()[:255]
    website = _norm_url(raw.get("website"))
    cc = (raw.get("country_code") or "").upper()[:2]
    finding = ResearchFinding(run=run, name=name or "(no name)", website=website[:500], country_code=cc, raw=raw, verification=v)
    if not name:
        finding.status, finding.reason = ResearchFinding.Status.REJECTED, "No company name returned."
        finding.save()
        return finding
    if raw.get("relevance") == "none":
        finding.status, finding.reason = ResearchFinding.Status.REJECTED, "Agent rated the company not relevant."
        finding.save()
        return finding
    country = Country.objects.filter(iso2=cc).first()
    emails = [c["email"] for c in v["contacts"] if c.get("email")]
    existing = crm.find_duplicate(name, website, cc, emails)
    if existing:
        before = (existing.contacts.count(), existing.sources.count())
        _apply_profile(existing, raw, v, overwrite=False)
        existing.save()
        _apply_sources(existing, v["sources"])
        verified, unverified = _apply_contacts(existing, v["contacts"])
        changed = (existing.contacts.count(), existing.sources.count()) != before
        finding.company = existing
        finding.status = ResearchFinding.Status.UPDATED if changed else ResearchFinding.Status.DUPLICATE
        finding.reason = f"Matches existing company #{existing.pk}."
        finding.save()
        run.verified_contacts += verified
        run.unverified_contacts += unverified
        return finding
    if not v["website"].get("ok"):
        finding.status = ResearchFinding.Status.NEEDS_REVIEW
        finding.reason = f"Website could not be verified ({v['website'].get('error') or 'unreachable'})."
        finding.save()
        return finding
    company = Company(name=name, website=website, country=country, lead_source=lead_source, research_run=run,
                      stage=Stage.RESEARCHED)
    _apply_profile(company, raw, v, overwrite=True)
    company.save()
    _apply_sources(company, v["sources"])
    verified, unverified = _apply_contacts(company, v["contacts"])
    if verified:
        crm.advance_stage(company, Stage.VERIFIED, agent=AgentName.BUYER_RESEARCH,
                          reason="Website active and at least one published e-mail verified")
    else:
        crm.log_activity(company, "system", "Added by research agent",
                         "No verified e-mail yet - outreach is blocked until a contact is verified.",
                         agent=AgentName.BUYER_RESEARCH)
    finding.company = company
    finding.status = ResearchFinding.Status.ADDED
    finding.save()
    run.verified_contacts += verified
    run.unverified_contacts += unverified
    return finding


def _finish(run: ResearchRun, tracker, status, summary="", error=""):
    run.status = status
    run.summary = summary
    run.error = error
    run.cost_usd = Decimal(tracker.run.cost_usd) if tracker else run.cost_usd
    run.web_searches = tracker.run.web_searches if tracker else run.web_searches
    run.finished_at = timezone.now()
    counts = run.findings.values_list("status", flat=True)
    run.found_count = len(counts)
    run.added_count = sum(1 for s in counts if s == ResearchFinding.Status.ADDED)
    run.duplicate_count = sum(1 for s in counts if s in (ResearchFinding.Status.DUPLICATE, ResearchFinding.Status.UPDATED))
    run.rejected_count = sum(1 for s in counts if s == ResearchFinding.Status.REJECTED)
    run.review_count = sum(1 for s in counts if s == ResearchFinding.Status.NEEDS_REVIEW)
    run.save()


def execute_run(run: ResearchRun, job=None, cache: PageCache | None = None) -> ResearchRun:
    """Execute a buyer / OEM / deep-dive / country research run."""
    if run.status in (ResearchRun.Status.COMPLETED, ResearchRun.Status.CANCELLED):
        return run
    sys = SystemSettings.get_solo()
    cache = cache or PageCache()
    run.status = ResearchRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])
    agent = {ResearchRun.Kind.BUYER: AgentName.BUYER_RESEARCH, ResearchRun.Kind.OEM: AgentName.OEM_RESEARCH,
             ResearchRun.Kind.DEEP_DIVE: AgentName.OEM_RESEARCH, ResearchRun.Kind.COUNTRY: AgentName.COUNTRY}[run.kind]
    tracker = None
    try:
        with agent_run(agent, f"research.{run.kind}", run, job) as tracker:
            if run.kind == ResearchRun.Kind.COUNTRY:
                _country_report(run, tracker, sys)
            elif run.kind == ResearchRun.Kind.DEEP_DIVE:
                _deep_dive(run, tracker, sys, cache)
            else:
                _discovery(run, tracker, sys, cache)
            tracker.run.summary = run.summary
    except Exception as exc:
        _finish(run, tracker, ResearchRun.Status.FAILED, run.summary, f"{exc.__class__.__name__}: {exc}")
        audit("research.failed", run, agent=agent, summary=str(exc)[:300])
        raise
    _finish(run, tracker, ResearchRun.Status.COMPLETED, run.summary)
    audit("research.completed", run, agent=agent,
          summary=f"{run.get_kind_display()}: {run.added_count} added, {run.duplicate_count} duplicates, {run.review_count} for review")
    return run


def _discovery(run: ResearchRun, tracker, sys: SystemSettings, cache: PageCache):
    remaining = sys.research_daily_company_limit - companies_added_today()
    if remaining <= 0:
        tracker.skip("Daily research limit reached")
        run.summary = "Skipped: daily company research limit reached."
        return
    count = max(1, min(run.target_count, remaining))
    countries = list(run.countries.all())
    exclude = set(
        Company.objects.filter(country__in=countries).exclude(domain="").values_list("domain", flat=True)
    ) if countries else set()
    oem = run.kind == ResearchRun.Kind.OEM
    data, seen = research_call(
        tracker, model=sys.ai_model_research, effort=sys.ai_effort_research,
        system=prompts.OEM_SYSTEM if oem else prompts.BUYER_SYSTEM,
        prompt=prompts.buyer_prompt(countries=countries, segments=run.segments, product_focus=list(run.product_focus.all()),
                                    company_types=run.company_types, count=count, exclude_domains=exclude,
                                    instructions=run.instructions, oem=oem),
        submit_tool=prompts.SUBMIT_COMPANIES_TOOL,
        max_searches=sys.research_max_web_searches, max_fetches=sys.research_max_web_searches,
    )
    lead_source = LeadSource.AI_OEM if oem else LeadSource.AI_RESEARCH
    for raw in (data.get("companies") or [])[:count]:
        ensure_not_stopped()
        domain = domain_from_url(raw.get("website", ""))
        if domain in exclude:
            ResearchFinding.objects.create(run=run, name=raw.get("name", "")[:255], website=raw.get("website", "")[:500],
                                           raw=raw, status=ResearchFinding.Status.DUPLICATE, reason="Domain already in CRM.",
                                           company=Company.objects.filter(domain=domain).first())
            continue
        v = verify_company_payload(raw, cache, seen)
        ingest_company(run, raw, v, lead_source=lead_source)
    for c in countries:
        Country.objects.filter(pk=c.pk).update(last_researched_at=timezone.now())
    run.summary = (data.get("search_notes") or "")[:4000]


def _deep_dive(run: ResearchRun, tracker, sys: SystemSettings, cache: PageCache):
    company = run.company
    data, seen = research_call(
        tracker, model=sys.ai_model_research, effort=sys.ai_effort_research, system=prompts.OEM_SYSTEM,
        prompt=prompts.deep_dive_prompt(company), submit_tool=prompts.SUBMIT_PROFILE_TOOL,
        max_searches=max(3, sys.research_max_web_searches // 2), max_fetches=sys.research_max_web_searches,
    )
    raw = data.get("company") or {}
    raw.setdefault("name", company.name)
    if not raw.get("website"):
        raw["website"] = company.website
    v = verify_company_payload(raw, cache, seen)
    with transaction.atomic():
        _apply_profile(company, raw, v, overwrite=True)
        company.save()
        _apply_sources(company, v["sources"])
        verified, unverified = _apply_contacts(company, v["contacts"])
        ResearchFinding.objects.create(run=run, name=company.name, website=company.website, raw=raw, verification=v,
                                       status=ResearchFinding.Status.UPDATED, company=company, reason="Deep-dive update")
        run.verified_contacts += verified
        run.unverified_contacts += unverified
        if verified:
            crm.advance_stage(company, Stage.VERIFIED, agent=AgentName.OEM_RESEARCH, reason="Verified contact found in deep-dive")
    run.summary = (data.get("search_notes") or "")[:4000]


def _country_report(run: ResearchRun, tracker, sys: SystemSettings):
    country = run.countries.first()
    data, seen = research_call(
        tracker, model=sys.ai_model_research, effort=sys.ai_effort_research, system=prompts.COUNTRY_SYSTEM,
        prompt=prompts.country_prompt(country, list(run.product_focus.all())), submit_tool=prompts.SUBMIT_MARKET_TOOL,
        max_searches=sys.research_max_web_searches, max_fetches=sys.research_max_web_searches,
    )
    sources = [{**s, "seen_by_search": s.get("url") in seen} for s in data.get("sources") or [] if s.get("url")]
    confidence = data.get("confidence", "low")
    if not sources:
        confidence = "low"
    report = CountryMarketReport.objects.create(
        country=country, research_run=run, generated_by_ai=True, sources=sources, confidence=confidence,
        **{f: data.get(f, "") for f in ("summary", "demand_assessment", "buyer_types", "import_requirements",
                                         "relevant_standards", "competitors", "opportunities", "risks")},
        demand_level=data.get("demand_level", "unknown"),
    )
    unseen = sum(1 for s in sources if not s["seen_by_search"])
    run.summary = f"Market report #{report.pk} created ({len(sources)} sources, {unseen} not returned by the search tool)."
