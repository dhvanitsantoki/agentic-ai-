from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.forms import BootstrapFormMixin
from apps.core.permissions import editor_required, manager_required

from .models import Continent, Country, CountryMarketReport


def market_overview(request):
    continent = request.GET.get("continent") or ""
    countries = Country.objects.select_related("continent").annotate(
        n_companies=Count("companies", distinct=True),
        n_a=Count("companies", filter=Q(companies__oem_category="A"), distinct=True),
        n_b=Count("companies", filter=Q(companies__oem_category="B"), distinct=True),
        n_contacted=Count("companies", filter=Q(companies__last_contacted_at__isnull=False), distinct=True),
        n_replied=Count("companies", filter=Q(companies__last_reply_at__isnull=False), distinct=True),
        n_reports=Count("market_reports", distinct=True),
    )
    if continent:
        countries = countries.filter(continent_id=continent)
    show = request.GET.get("show", "active")
    if show == "active":
        countries = countries.filter(Q(n_companies__gt=0) | Q(n_reports__gt=0) | Q(is_priority=True))
    countries = countries.order_by("-is_priority", "-n_companies", "name")
    continents = Continent.objects.annotate(n=Count("countries__companies", distinct=True))
    return render(request, "geo/market_overview.html", {
        "countries": countries, "continents": continents, "continent": continent, "show": show,
        "all_countries": Country.objects.order_by("name"),
    })


class CountryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Country
        fields = ["timezone", "is_priority", "research_notes"]
        widgets = {"research_notes": forms.Textarea(attrs={"rows": 3})}


def country_detail(request, iso2):
    country = get_object_or_404(Country.objects.select_related("continent"), iso2=iso2.upper())
    companies = country.companies.select_related("owner").order_by("-oem_score", "name")[:100]
    reports = country.market_reports.all()[:10]
    by_category = dict(country.companies.values_list("oem_category").annotate(n=Count("id")))
    by_stage = country.companies.values("stage").annotate(n=Count("id")).order_by("stage")
    return render(request, "geo/country_detail.html", {
        "country": country, "companies": companies, "reports": reports, "by_category": by_category,
        "by_stage": by_stage, "form": CountryForm(instance=country),
        "runs": country.research_runs.order_by("-created_at")[:10],
    })


@editor_required
@require_POST
def country_update(request, iso2):
    country = get_object_or_404(Country, iso2=iso2.upper())
    form = CountryForm(request.POST, instance=country)
    if form.is_valid():
        form.save()
        audit("geo.country_update", country, user=request.user, summary=f"{country} settings updated", changed=form.changed_data)
        messages.success(request, "Country settings saved.")
    else:
        messages.error(request, "Invalid input.")
    return redirect("geo:country_detail", iso2=country.iso2)


def report_detail(request, pk):
    report = get_object_or_404(CountryMarketReport.objects.select_related("country", "reviewed_by", "research_run"), pk=pk)
    return render(request, "geo/report_detail.html", {"report": report})


@manager_required
@require_POST
def report_review(request, pk):
    report = get_object_or_404(CountryMarketReport, pk=pk)
    action = request.POST.get("action", "reviewed")
    if action == "archive":
        report.status = CountryMarketReport.Status.ARCHIVED
    else:
        report.status = CountryMarketReport.Status.REVIEWED
        report.reviewed_by = request.user
        report.reviewed_at = timezone.now()
    report.save()
    audit("geo.report_review", report, user=request.user, summary=f"Market report {pk}: {action}")
    messages.success(request, "Report updated.")
    return redirect("geo:report_detail", pk=pk)
