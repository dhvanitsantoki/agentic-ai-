"""Verification of manually added / imported companies and contacts (no AI needed)."""
from django.utils import timezone

from apps.crm.models import Company, Contact, Stage
from apps.crm import services as crm

from .verification import PageCache, check_website, crawl_published_contacts, verify_email


def verify_contact(contact: Contact, cache: PageCache | None = None) -> Contact:
    if not contact.email or contact.email_status in (Contact.EmailStatus.MANUAL, Contact.EmailStatus.BOUNCED):
        return contact
    cache = cache or PageCache()
    candidates = [u for u in [contact.email_source_url] if u]
    company = contact.company
    if company.website:
        published = crawl_published_contacts(company.website, cache)
        src = published["emails"].get(contact.email.lower())
        if src:
            candidates.insert(0, src)
    checks = {"result": "not_found_on_source"}
    for url in candidates or [""]:
        checks = verify_email(contact.email, url, cache)
        if checks.get("found_on_source"):
            break
    if not candidates:
        # still record syntax / MX results so the team sees why it is unverified
        checks = verify_email(contact.email, "", cache)
    contact.email_checks = {**checks, "checked_at": timezone.now().isoformat()}
    if checks["result"] == "verified":
        contact.email_status = Contact.EmailStatus.VERIFIED
        contact.email_source_url = checks.get("source_url") or contact.email_source_url
    elif checks["result"] == "invalid" or not checks.get("mx", True) or not checks.get("syntax", True):
        contact.email_status = Contact.EmailStatus.INVALID
    else:
        contact.email_status = Contact.EmailStatus.UNVERIFIED
    contact.save(update_fields=["email_checks", "email_status", "email_source_url", "updated_at"])
    if contact.email_status == Contact.EmailStatus.VERIFIED and company.verification_status == Company.VerificationStatus.VERIFIED:
        crm.advance_stage(company, Stage.VERIFIED, agent="crm", reason="Contact e-mail verified")
    return contact


def verify_company(company: Company, cache: PageCache | None = None) -> dict:
    cache = cache or PageCache()
    result = {"company": company.pk}
    if company.website:
        site = check_website(company.website, cache)
        company.verification_status = Company.VerificationStatus.VERIFIED if site["ok"] else Company.VerificationStatus.INACTIVE
        company.website_checked_at = timezone.now()
        company.save(update_fields=["verification_status", "website_checked_at", "updated_at"])
        result["website_ok"] = site["ok"]
        if site["ok"]:
            published = crawl_published_contacts(company.website, cache)
            known = {e.lower() for e in company.contacts.exclude(email="").values_list("email", flat=True)}
            added = 0
            for email, src in published["emails"].items():
                if email not in known:
                    checks = verify_email(email, src, cache)
                    if checks["result"] in ("verified", "invalid"):
                        Contact.objects.create(
                            company=company, email=email, email_source_url=src, email_checks=checks,
                            email_status=Contact.EmailStatus.VERIFIED if checks["result"] == "verified" else Contact.EmailStatus.INVALID,
                        )
                        added += 1
            result["published_emails_added"] = added
    for contact in company.contacts.exclude(email=""):
        verify_contact(contact, cache)
    if company.stage in (Stage.NEW, Stage.RESEARCHED):
        if company.verification_status == Company.VerificationStatus.VERIFIED and company.contacts.filter(
            email_status__in=[Contact.EmailStatus.VERIFIED, Contact.EmailStatus.MANUAL]
        ).exists():
            crm.advance_stage(company, Stage.VERIFIED, agent="crm", reason="Website and contact verified")
    return result
