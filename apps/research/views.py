from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.agents.llm import ai_configured, not_configured_message
from apps.agents.registry import enqueue
from apps.core.audit import audit
from apps.core.permissions import editor_required
from apps.crm.models import Company, LeadSource
from apps.geo.models import Country

from .forms import ResearchRunForm
from .models import ResearchFinding, ResearchRun
from .pipeline import _apply_contacts, _apply_profile, _apply_sources


def run_list(request):
    qs = ResearchRun.objects.prefetch_related("countries").select_related("triggered_by", "company")
    if request.GET.get("kind"):
        qs = qs.filter(kind=request.GET["kind"])
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "research/run_list.html", {"page_obj": page, "kinds": ResearchRun.Kind.choices,
                                                      "kind": request.GET.get("kind", "")})


def _queue_run(run, user):
    job = enqueue("research.run", {"run_id": run.pk}, user=user, max_attempts=1)
    run.job = job
    run.save(update_fields=["job"])
    return job


@editor_required
def run_create(request):
    initial = {}
    if request.GET.get("country"):
        initial["countries"] = [request.GET["country"].upper()]
    if request.GET.get("kind") in ("buyer", "oem"):
        initial["kind"] = request.GET["kind"]
    form = ResearchRunForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        if not ai_configured("research"):
            messages.error(request, not_configured_message("research"))
            return redirect("research:run_list")
        d = form.cleaned_data
        with transaction.atomic():
            run = ResearchRun.objects.create(kind=d["kind"], segments=d["segments"], company_types=d["company_types"],
                                             target_count=d["target_count"], instructions=d["instructions"],
                                             triggered_by=request.user)
            run.countries.set(d["countries"])
            run.product_focus.set(d["product_focus"])
        _queue_run(run, request.user)
        audit("research.queued", run, user=request.user, summary=f"{run.get_kind_display()} queued for {run.country_names}")
        messages.success(request, "Research run queued. Results appear here as the agent works (usually a few minutes).")
        return redirect("research:run_detail", pk=run.pk)
    return render(request, "core/form_page.html", {
        "form": form, "title": "New research run", "submit_label": "Start research",
        "subtitle": "The agent searches the web, then every company and contact is verified before it reaches the CRM.",
    })


def run_detail(request, pk):
    run = get_object_or_404(ResearchRun.objects.prefetch_related("countries", "product_focus"), pk=pk)
    findings = run.findings.select_related("company").order_by("status", "name")
    return render(request, "research/run_detail.html", {"run": run, "findings": findings,
                                                        "market_reports": run.market_reports.all()})


@editor_required
@require_POST
def run_cancel(request, pk):
    run = get_object_or_404(ResearchRun, pk=pk)
    if run.status == ResearchRun.Status.QUEUED:
        run.status = ResearchRun.Status.CANCELLED
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        if run.job and run.job.status == "queued":
            run.job.status = "cancelled"
            run.job.save(update_fields=["status"])
        messages.info(request, "Research run cancelled.")
    else:
        messages.warning(request, "Only queued runs can be cancelled.")
    return redirect("research:run_detail", pk=pk)


@editor_required
@require_POST
def deep_dive(request, pk):
    company = get_object_or_404(Company, pk=pk)
    if not ai_configured("research"):
        messages.error(request, not_configured_message("research"))
        return redirect(company)
    run = ResearchRun.objects.create(kind=ResearchRun.Kind.DEEP_DIVE, company=company, target_count=1, triggered_by=request.user)
    if company.country_id:
        run.countries.set([company.country])
    _queue_run(run, request.user)
    messages.success(request, "OEM deep-dive queued. The company profile will be updated when it finishes.")
    return redirect("research:run_detail", pk=run.pk)


@editor_required
@require_POST
def country_report(request, iso2):
    country = get_object_or_404(Country, iso2=iso2.upper())
    if not ai_configured("research"):
        messages.error(request, not_configured_message("research"))
        return redirect("geo:country_detail", iso2=country.iso2)
    run = ResearchRun.objects.create(kind=ResearchRun.Kind.COUNTRY, target_count=1, triggered_by=request.user)
    run.countries.set([country])
    _queue_run(run, request.user)
    messages.success(request, f"Market research for {country.name} queued.")
    return redirect("research:run_detail", pk=run.pk)


def review_queue(request):
    findings = ResearchFinding.objects.filter(status=ResearchFinding.Status.NEEDS_REVIEW).select_related("run")
    page = Paginator(findings, 30).get_page(request.GET.get("page"))
    return render(request, "research/review_queue.html", {"page_obj": page})


@editor_required
@require_POST
def finding_review(request, pk):
    finding = get_object_or_404(ResearchFinding, pk=pk, status=ResearchFinding.Status.NEEDS_REVIEW)
    action = request.POST.get("action")
    finding.reviewed_by = request.user
    finding.reviewed_at = timezone.now()
    if action == "approve":
        raw, v = finding.raw, finding.verification
        country = Country.objects.filter(iso2=finding.country_code).first()
        with transaction.atomic():
            company = Company(name=finding.name, website=finding.website, country=country, research_run=finding.run,
                              lead_source=LeadSource.AI_OEM if finding.run.kind == "oem" else LeadSource.AI_RESEARCH,
                              owner=request.user)
            _apply_profile(company, raw, v, overwrite=True)
            company.save()
            _apply_sources(company, v.get("sources", []))
            _apply_contacts(company, v.get("contacts", []))
            finding.company = company
            finding.status = ResearchFinding.Status.ADDED
            finding.reason += f" Approved by {request.user.username}."
            finding.save()
        audit("research.finding_approved", company, user=request.user, summary=f"{company} added from review queue")
        messages.success(request, f"{company.name} added to the CRM.")
    else:
        finding.status = ResearchFinding.Status.REJECTED
        finding.reason += f" Rejected by {request.user.username}."
        finding.save()
        messages.info(request, "Finding rejected.")
    return redirect("research:review_queue")
