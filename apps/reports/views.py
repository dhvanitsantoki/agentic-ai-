import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.agents.llm import month_spend
from apps.agents.models import AgentRun
from apps.core.models import CompanyProfile, SystemSettings
from apps.core.permissions import manager_required
from apps.crm.models import Company, OemCategory, Stage, Task
from apps.followups.models import Enrollment
from apps.mailbox.models import InboundEmail, MailAccount
from apps.outreach.models import OutboundEmail
from apps.products.models import PriceList
from apps.quotations.models import Quotation

from . import metrics
from .exports import report_to_pdf, report_to_xlsx
from .models import ReportSnapshot
from .services import generate_report


def dashboard(request):
    today = timezone.localdate()
    week = metrics.compute(today - timezone.timedelta(days=6), today)
    today_m = metrics.compute(today, today)
    sys = SystemSettings.get_solo()
    series = metrics.daily_series(30)
    funnel = [f for f in metrics.funnel() if f["stage"] != Stage.LOST]
    profile = CompanyProfile.get_solo()
    setup_missing = []
    if profile.missing_for_outreach():
        setup_missing.append("Complete the company profile")
    if not MailAccount.objects.filter(is_active=True).exists():
        setup_missing.append("Connect a mailbox")
    if not PriceList.objects.filter(status=PriceList.Status.APPROVED).exists():
        setup_missing.append("Add products and an approved price list")
    return render(request, "reports/dashboard.html", {
        "week": week, "today_m": today_m, "sys": sys, "spend": month_spend(),
        "series_json": json.dumps(series), "funnel": funnel, "funnel_json": json.dumps(funnel),
        "quality": metrics.lead_quality(), "quality_labels": dict(OemCategory.choices),
        "top_countries": metrics.top_countries(8),
        "approvals": OutboundEmail.objects.filter(status=OutboundEmail.Status.PENDING_APPROVAL).select_related("company")[:6],
        "quotes_pending": Quotation.objects.filter(status__in=[Quotation.Status.PENDING_APPROVAL, Quotation.Status.NEEDS_INPUT]).select_related("company")[:6],
        "hot": Company.objects.filter(stage__in=[Stage.INTERESTED, Stage.REQUIREMENT, Stage.NEGOTIATION, Stage.SAMPLE]).select_related("country")[:8],
        "recent_replies": InboundEmail.objects.exclude(classification__in=metrics.NON_HUMAN).select_related("company")[:6],
        "followups_due": Enrollment.objects.filter(status=Enrollment.Status.ACTIVE, next_due_at__lte=timezone.now()).count(),
        "my_tasks": Task.objects.filter(done=False, assigned_to=request.user).select_related("company")[:6],
        "errors": AgentRun.objects.filter(status=AgentRun.Status.FAILED).order_by("-started_at")[:5],
        "setup_missing": setup_missing,
    })


def report_list(request):
    period = request.GET.get("period", "")
    qs = ReportSnapshot.objects.all()
    if period:
        qs = qs.filter(period=period)
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "reports/list.html", {"page_obj": page, "period": period, "periods": ReportSnapshot.Period.choices})


def report_detail(request, pk):
    snap = get_object_or_404(ReportSnapshot, pk=pk)
    m = snap.metrics
    scalar = {k: v for k, v in m.items() if isinstance(v, (int, float))}
    return render(request, "reports/detail.html", {"snap": snap, "m": m, "scalar": scalar})


@manager_required
@require_POST
def report_generate(request):
    period = request.POST.get("period", "daily")
    if period not in ReportSnapshot.Period.values:
        period = "daily"
    snap = generate_report(period, narrative=request.POST.get("narrative") == "1", email=request.POST.get("email") == "1")
    messages.success(request, f"{snap.get_period_display()} report generated.")
    return redirect("reports:detail", pk=snap.pk)


def report_export(request, pk, fmt):
    snap = get_object_or_404(ReportSnapshot, pk=pk)
    if fmt == "xlsx":
        return report_to_xlsx(snap)
    resp = HttpResponse(report_to_pdf(snap), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="report-{snap.period}-{snap.start}.pdf"'
    return resp
