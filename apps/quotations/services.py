"""Agent 6: quotations. Prices come only from approved price lists; anything missing goes to a human."""
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from apps.core.audit import audit
from apps.core.models import CompanyProfile
from apps.core.permissions import is_manager
from apps.crm import services as crm
from apps.crm.models import Requirement, Stage
from apps.products.services import get_approved_price, match_variants

from .models import Quotation, QuotationLine

REQUIRED_TERMS = {
    "currency": "Currency", "incoterm": "Incoterm", "payment_terms": "Payment terms", "lead_time": "Lead time",
    "valid_until": "Validity date", "contact": "Customer contact",
}


def compute_missing(q: Quotation) -> list[str]:
    missing = [label for field, label in REQUIRED_TERMS.items() if not getattr(q, field if field != "contact" else "contact_id")]
    if q.freight_amount is None and not q.freight_note.strip():
        missing.append("Freight (amount or note)")
    if q.incoterm and q.incoterm != "EXW" and not q.port_of_loading:
        missing.append("Port of loading")
    if q.incoterm in ("CFR", "CIF", "CPT", "CIP", "DAP", "DDP") and not q.destination:
        missing.append("Destination")
    lines = list(q.lines.all())
    if not lines:
        missing.append("At least one product line")
    for line in lines:
        if line.variant_id is None:
            missing.append(f"Line {line.position}: match to a verified product size")
        if line.unit_price is None:
            missing.append(f"Line {line.position}: approved price")
        if line.below_moq:
            missing.append(f"Line {line.position}: quantity below MOQ ({line.moq}) - confirm or adjust")
    return missing


def refresh_status(q: Quotation, save=True) -> Quotation:
    if q.is_locked or q.status in (Quotation.Status.CANCELLED, Quotation.Status.PENDING_APPROVAL):
        return q
    q.missing_fields = compute_missing(q)
    q.status = Quotation.Status.NEEDS_INPUT if q.missing_fields else Quotation.Status.DRAFT
    if save:
        q.save(update_fields=["missing_fields", "status", "updated_at"])
    return q


def apply_defaults(q: Quotation):
    p = CompanyProfile.get_solo()
    q.incoterm = q.incoterm or p.default_incoterm
    q.payment_terms = q.payment_terms or p.default_payment_terms
    q.lead_time = q.lead_time or p.default_lead_time
    q.port_of_loading = q.port_of_loading or p.default_port_of_loading
    q.packaging = q.packaging or p.default_packaging
    if not q.valid_until and p.quotation_validity_days:
        q.valid_until = timezone.localdate() + timezone.timedelta(days=p.quotation_validity_days)


def price_line(line: QuotationLine, currency: str):
    """Fill the unit price from the approved price list (never invented). Leaves None if unavailable."""
    if line.variant_id is None or line.price_overridden_by_id:
        return
    result = get_approved_price(line.variant, currency, line.quantity)
    line.unit_price = result.unit_price if result else None
    line.price_item = result.item if result else None
    v = line.variant
    line.moq = v.effective_moq
    line.packaging = line.packaging or v.effective_packaging
    line.size = line.size or v.size
    line.material = line.material or v.product.material
    line.pressure_rating = line.pressure_rating or v.effective_pressure_rating
    line.connection_type = line.connection_type or v.connection_type or v.product.connection_types
    if not line.description:
        line.description = f"{v.product.name} {v.size}"


def reprice(q: Quotation):
    for line in q.lines.select_related("variant__product"):
        price_line(line, q.currency)
        line.save()
    refresh_status(q)


def _parse_qty(text: str) -> int:
    import re

    m = re.search(r"\d[\d,\.]*", text or "")
    if not m:
        return 0
    raw = m.group(0).replace(",", "")
    try:
        qty = int(float(raw))
    except ValueError:
        return 0
    if re.search(r"\b(k|thousand)\b", (text or "").lower()):
        qty *= 1000
    return qty


@transaction.atomic
def create_quotation(company, *, contact=None, currency="USD", requirements=(), user=None, agent=False, source_email=None) -> Quotation:
    q = Quotation(company=company, contact=contact or company.primary_contact, currency=currency, created_by=user,
                  created_by_agent=agent, source_email=source_email)
    apply_defaults(q)
    q.save()
    pos = 1
    for req in requirements:
        candidates = match_variants(material=req.material, category_hint=req.product_category.name if req.product_category_id else "",
                                    size=req.sizes, text=req.description)
        variant = candidates[0] if len(candidates) == 1 else None
        line = QuotationLine(quotation=q, position=pos, variant=variant, quantity=max(_parse_qty(req.quantity), 1),
                             customer_request=f"{req.description} {req.material} {req.sizes} {req.quantity}".strip()[:500],
                             description=(f"{variant.product.name} {variant.size}" if variant else req.description[:300]),
                             material=req.material[:20], size=req.sizes[:60], pressure_rating=req.pressure_rating[:80],
                             connection_type=req.connection_type[:120])
        price_line(line, currency)
        line.save()
        pos += 1
        if q.requirement_id is None:
            q.requirement = req
    q.save()
    refresh_status(q)
    audit("quotation.created", q, user=user, agent="quotation" if agent else "",
          summary=f"{q.number} for {company} ({q.lines.count()} lines, {len(q.missing_fields)} missing items)")
    return q


def draft_from_inbound(inbound, requirements, analysis) -> Quotation | None:
    """Called by Agent 4 when a buyer asks for a quotation."""
    company = inbound.company
    if company is None:
        return None
    if Quotation.objects.filter(source_email=inbound).exists():
        return None
    currency = (analysis.get("currency_mentioned") or "").upper()[:3]
    if currency not in {"USD", "EUR", "INR", "GBP", "AED", "AUD"}:
        currency = "USD"
    reqs = requirements or list(Requirement.objects.filter(source_email=inbound))
    q = create_quotation(company, contact=inbound.contact, currency=currency, requirements=reqs, agent=True, source_email=inbound)
    if analysis.get("destination_or_incoterm"):
        q.internal_notes = f"Buyer mentioned: {analysis['destination_or_incoterm']}"
        q.save(update_fields=["internal_notes"])
    crm.escalate(company, f"Quotation requested: {company.name}",
                 f"Draft {q.number} prepared from the buyer's e-mail. Missing: {', '.join(q.missing_fields) or 'nothing'}. "
                 "A manager must complete and approve it before it can be sent.", link=f"/quotations/{q.pk}/", agent="quotation")
    return q


def submit_for_approval(q: Quotation, user):
    refresh_status(q)
    if q.missing_fields:
        raise ValidationError("Complete the missing items first: " + "; ".join(q.missing_fields))
    q.status = Quotation.Status.PENDING_APPROVAL
    q.save(update_fields=["status", "updated_at"])
    audit("quotation.submitted", q, user=user, summary=f"{q.number} submitted for approval")


def approve(q: Quotation, user):
    if not is_manager(user):
        raise PermissionDenied("Only a manager or admin can approve quotations.")
    q.missing_fields = compute_missing(q)
    if q.missing_fields:
        raise ValidationError("Cannot approve - missing: " + "; ".join(q.missing_fields))
    if q.status not in (Quotation.Status.PENDING_APPROVAL, Quotation.Status.DRAFT):
        raise ValidationError("Only draft or submitted quotations can be approved.")
    from .pdf import render_quotation_pdf

    q.status = Quotation.Status.APPROVED
    q.approved_by = user
    q.approved_at = timezone.now()
    q.save()
    pdf = render_quotation_pdf(q)
    q.pdf.save(f"{q.number.replace('/', '-')}.pdf", ContentFile(pdf), save=True)
    audit("quotation.approved", q, user=user, summary=f"{q.number} approved, total {q.total} {q.currency}")


def set_discount(q: Quotation, percent: Decimal, user):
    if not is_manager(user):
        raise PermissionDenied("Only a manager can set discounts.")
    if q.is_locked:
        raise ValidationError("Approved quotations cannot be changed. Create a revision.")
    q.discount_percent = percent
    q.discount_set_by = user
    q.save(update_fields=["discount_percent", "discount_set_by", "updated_at"])
    audit("quotation.discount", q, user=user, summary=f"{q.number} discount {percent}%")


def override_price(line: QuotationLine, price: Decimal, user):
    if not is_manager(user):
        raise PermissionDenied("Only a manager can change a price.")
    if line.quotation.is_locked:
        raise ValidationError("Approved quotations cannot be changed.")
    line.unit_price = price
    line.price_overridden_by = user
    line.price_item = None
    line.save()
    audit("quotation.price_override", line.quotation, user=user, summary=f"Line {line.position}: {price} {line.quotation.currency}")
    refresh_status(line.quotation)


@transaction.atomic
def new_revision(original: Quotation, user) -> Quotation:
    """Copy an approved/sent quotation into a new editable draft (the original stays unchanged)."""
    q = Quotation.objects.get(pk=original.pk)
    lines = list(q.lines.all())
    base_number = original.number.split("-R")[0]
    latest = Quotation.objects.filter(number__startswith=base_number).order_by("-revision").values_list("revision", flat=True).first() or 0
    q.pk = None
    q.number = ""
    q.revision = latest + 1
    q.status = Quotation.Status.DRAFT
    q.approved_by = q.approved_at = q.sent_at = None
    q.pdf = None
    q.created_by = user
    q.save()
    q.number = f"{base_number}-R{q.revision}"
    q.save(update_fields=["number"])
    for line in lines:
        line.pk = None
        line.quotation = q
        line.save()
    refresh_status(q)
    audit("quotation.revision", q, user=user, summary=f"Revision {q.number} of {original.number}")
    return q


def send_quotation(q: Quotation, user):
    """Create the e-mail carrying the approved quotation PDF (sending still goes through policy)."""
    from apps.core import policy
    from apps.outreach.drafting import signature
    from apps.outreach.models import EmailKind, OutboundEmail

    if q.status != Quotation.Status.APPROVED or not q.pdf:
        raise ValidationError("Only approved quotations with a generated PDF can be sent.")
    if not is_manager(user):
        raise PermissionDenied("Only a manager can send quotations.")
    contact = q.contact
    name = contact.full_name if contact and contact.full_name else "Sir/Madam"
    body = (f"Dear {name},\n\nThank you for your enquiry. Please find attached our quotation {q.number}"
            f"{' valid until ' + q.valid_until.strftime('%d %B %Y') if q.valid_until else ''}.\n\n"
            "Please let us know if you would like any changes to sizes, quantities or packaging.\n\nBest regards,\n\n"
            + signature(user))
    email = OutboundEmail.objects.create(
        company=q.company, contact=contact, to_email=contact.email if contact else "", kind=EmailKind.QUOTATION,
        subject=f"Quotation {q.number} - {CompanyProfile.get_solo().brand_name}", body_text=body, quotation=q,
        in_reply_to=q.source_email, account=q.source_email.account if q.source_email_id else None, created_by=user,
    )
    policy.apply(email)
    return email


def expire_quotations() -> int:
    today = timezone.localdate()
    return Quotation.objects.filter(status__in=[Quotation.Status.SENT, Quotation.Status.APPROVED], valid_until__lt=today).update(
        status=Quotation.Status.EXPIRED)


def mark_outcome(q: Quotation, accepted: bool, user):
    q.status = Quotation.Status.ACCEPTED if accepted else Quotation.Status.REJECTED
    q.save(update_fields=["status", "updated_at"])
    if accepted:
        crm.advance_stage(q.company, Stage.ORDER, user=user, reason=f"Quotation {q.number} accepted")
    audit("quotation.outcome", q, user=user, summary=f"{q.number} {'accepted' if accepted else 'rejected'}")
