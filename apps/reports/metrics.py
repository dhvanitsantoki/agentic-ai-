"""Report metrics computed directly from the database (no estimates, no AI)."""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.agents.models import AgentRun
from apps.crm.models import Activity, Company, Stage, StageHistory, Task
from apps.mailbox.models import Classification, InboundEmail
from apps.outreach.models import EmailKind, OutboundEmail
from apps.quotations.models import Quotation, QuotationLine
from apps.research.models import ResearchRun

PIPELINE_OPEN = [Stage.INTERESTED, Stage.REQUIREMENT, Stage.QUOTED, Stage.NEGOTIATION, Stage.SAMPLE]
NON_HUMAN = [Classification.BOUNCE, Classification.AUTO_REPLY, Classification.UNRELATED, Classification.PENDING]


def period_bounds(period: str, ref=None):
    """Return (start_date, end_date) of the last complete period before `ref` (default: today)."""
    today = ref or timezone.localdate()
    if period == "daily":
        d = today - timedelta(days=1)
        return d, d
    if period == "weekly":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    first_this_month = today.replace(day=1)
    last_prev = first_this_month - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


def _range(start, end):
    tz = timezone.get_current_timezone()
    return datetime.combine(start, time.min, tz), datetime.combine(end + timedelta(days=1), time.min, tz)


def _money_by_currency(rows):
    out = defaultdict(Decimal)
    for currency, value in rows:
        if value is not None:
            out[currency or "USD"] += Decimal(value)
    return {k: float(v) for k, v in sorted(out.items())}


def compute(start, end) -> dict:
    a, b = _range(start, end)
    sent = OutboundEmail.objects.filter(status=OutboundEmail.Status.SENT, sent_at__gte=a, sent_at__lt=b)
    inbound = InboundEmail.objects.filter(received_at__gte=a, received_at__lt=b)
    human_in = inbound.exclude(classification__in=NON_HUMAN)
    stage_moves = StageHistory.objects.filter(changed_at__gte=a, changed_at__lt=b)
    new_companies = Company.objects.filter(created_at__gte=a, created_at__lt=b)
    quotes_sent = Quotation.objects.filter(sent_at__gte=a, sent_at__lt=b)
    n_sent = sent.count()
    n_bounced = OutboundEmail.objects.filter(bounced_at__gte=a, bounced_at__lt=b).count()
    m = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "new_companies": new_companies.count(),
        "new_companies_ai": new_companies.filter(lead_source__in=["ai_research", "ai_oem"]).count(),
        "verified_buyers": stage_moves.filter(to_stage=Stage.VERIFIED).values("company").distinct().count(),
        "research_runs": ResearchRun.objects.filter(created_at__gte=a, created_at__lt=b).count(),
        "emails_sent": n_sent,
        "emails_sent_by_kind": dict(sent.values_list("kind").annotate(n=Count("id"))),
        "emails_bounced": n_bounced,
        "emails_not_bounced": max(n_sent - n_bounced, 0),
        "delivery_note": "Gmail, Outlook and Zoho APIs do not confirm delivery; 'not bounced' = sent minus bounce notices received.",
        "replies_received": human_in.count(),
        "replies_by_class": dict(human_in.values_list("classification").annotate(n=Count("id"))),
        "interested": human_in.filter(classification=Classification.INTERESTED).count(),
        "quotations_requested": human_in.filter(classification=Classification.NEED_QUOTATION).count(),
        "quotations_sent": quotes_sent.count(),
        "quotations_sent_value": _money_by_currency(
            [(q.currency, q.total) for q in quotes_sent.prefetch_related("lines")]),
        "followups_sent": sent.filter(kind=EmailKind.FOLLOWUP).count(),
        "meetings_logged": Activity.objects.filter(kind=Activity.Kind.MEETING, occurred_at__gte=a, occurred_at__lt=b).count(),
        "orders_confirmed": stage_moves.filter(to_stage=Stage.ORDER).values("company").distinct().count(),
        "complaints": human_in.filter(classification=Classification.COMPLAINT).count(),
        "open_pipeline_value": _money_by_currency(
            Company.objects.filter(stage__in=PIPELINE_OPEN).values_list("expected_value_currency", "expected_order_value")),
        "open_pipeline_companies": Company.objects.filter(stage__in=PIPELINE_OPEN).count(),
        "pending_email_approvals": OutboundEmail.objects.filter(status=OutboundEmail.Status.PENDING_APPROVAL).count(),
        "pending_quotation_approvals": Quotation.objects.filter(status=Quotation.Status.PENDING_APPROVAL).count(),
        "quotations_needing_input": Quotation.objects.filter(status=Quotation.Status.NEEDS_INPUT).count(),
        "agent_errors": AgentRun.objects.filter(status=AgentRun.Status.FAILED, started_at__gte=a, started_at__lt=b).count(),
        "agent_error_samples": list(AgentRun.objects.filter(status=AgentRun.Status.FAILED, started_at__gte=a, started_at__lt=b)
                                    .values("agent", "action", "error")[:10]),
        "ai_cost_usd": float(AgentRun.objects.filter(started_at__gte=a, started_at__lt=b).aggregate(s=Sum("cost_usd"))["s"] or 0),
        "tasks_open_overdue": Task.objects.filter(done=False, due_date__lt=timezone.localdate()).count(),
    }
    # country-wise
    countries = defaultdict(lambda: defaultdict(int))
    for name, n in new_companies.values_list("country__name").annotate(n=Count("id")):
        countries[name or "Unknown"]["new_companies"] = n
    for name, n in sent.values_list("company__country__name").annotate(n=Count("id")):
        countries[name or "Unknown"]["emails_sent"] = n
    for name, n in human_in.values_list("company__country__name").annotate(n=Count("id")):
        countries[name or "Unknown"]["replies"] = n
    for name, n in human_in.filter(classification__in=[Classification.INTERESTED, Classification.NEED_QUOTATION]).values_list(
            "company__country__name").annotate(n=Count("id")):
        countries[name or "Unknown"]["interested"] = n
    for name, n in quotes_sent.values_list("company__country__name").annotate(n=Count("id")):
        countries[name or "Unknown"]["quotations_sent"] = n
    m["by_country"] = sorted(({"country": k, **v} for k, v in countries.items()),
                             key=lambda r: (-r.get("replies", 0), -r.get("emails_sent", 0), r["country"]))
    # product-wise
    products = defaultdict(lambda: defaultdict(int))
    for name, n in Company.objects.filter(products_interested__isnull=False, updated_at__gte=a, updated_at__lt=b).values_list(
            "products_interested__name").annotate(n=Count("id")):
        products[name]["companies_interested"] = n
    for name, n in QuotationLine.objects.filter(quotation__sent_at__gte=a, quotation__sent_at__lt=b, variant__isnull=False).values_list(
            "variant__product__category__name").annotate(n=Count("id")):
        products[name]["quoted_lines"] = n
    m["by_product"] = sorted(({"category": k, **v} for k, v in products.items()), key=lambda r: r["category"])
    # employee-wise
    employees = defaultdict(lambda: defaultdict(int))
    for name, n in sent.exclude(approved_by=None).values_list("approved_by__username").annotate(n=Count("id")):
        employees[name]["emails_approved_sent"] = n
    for name, n in Quotation.objects.filter(approved_at__gte=a, approved_at__lt=b).values_list("approved_by__username").annotate(n=Count("id")):
        employees[name]["quotations_approved"] = n
    for name, n in Task.objects.filter(done_at__gte=a, done_at__lt=b).exclude(assigned_to=None).values_list(
            "assigned_to__username").annotate(n=Count("id")):
        employees[name]["tasks_completed"] = n
    for name, n in Company.objects.filter(stage__in=PIPELINE_OPEN).exclude(owner=None).values_list("owner__username").annotate(n=Count("id")):
        employees[name]["open_opportunities"] = n
    m["by_employee"] = sorted(({"employee": k, **v} for k, v in employees.items()), key=lambda r: r["employee"])
    m["auto_sent"] = sent.filter(approved_by__isnull=True).count()
    return m


def daily_series(days: int = 30) -> dict:
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    a, _ = _range(start, today)
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    sent, replies, leads = defaultdict(int), defaultdict(int), defaultdict(int)
    for dt in OutboundEmail.objects.filter(status="sent", sent_at__gte=a).values_list("sent_at", flat=True):
        sent[timezone.localtime(dt).date().isoformat()] += 1
    for dt in InboundEmail.objects.filter(received_at__gte=a).exclude(classification__in=NON_HUMAN).values_list("received_at", flat=True):
        replies[timezone.localtime(dt).date().isoformat()] += 1
    for dt in Company.objects.filter(created_at__gte=a).values_list("created_at", flat=True):
        leads[timezone.localtime(dt).date().isoformat()] += 1
    return {"labels": labels, "sent": [sent[d] for d in labels], "replies": [replies[d] for d in labels],
            "leads": [leads[d] for d in labels]}


def funnel() -> list[dict]:
    counts = dict(Company.objects.values_list("stage").annotate(n=Count("id")))
    return [{"stage": s.value, "label": s.label, "count": counts.get(s.value, 0)} for s in Stage]


def lead_quality() -> dict:
    return dict(Company.objects.exclude(oem_category="").values_list("oem_category").annotate(n=Count("id")))


def top_countries(limit=10) -> list:
    return list(Company.objects.exclude(country=None).values("country__name").annotate(
        n=Count("id"), replied=Count("id", filter=Q(last_reply_at__isnull=False))).order_by("-n")[:limit])
