import csv
import io
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.permissions import editor_required, is_manager, safe_next
from apps.geo.models import Continent, Country
from apps.products.models import ProductCategory

from . import services
from .forms import ActivityForm, CompanyForm, CompanyImportForm, ContactForm, RequirementForm, StageForm, TaskForm
from .models import (
    Activity, BusinessType, Company, CompanyType, Contact, LeadSource, OemCategory, Requirement, Stage, STAGE_ORDER, Task,
)

User = get_user_model()


def filtered_companies(params):
    """Shared filter logic for the company list, the pipeline and Excel exports."""
    from apps.followups.models import Enrollment
    from apps.quotations.models import Quotation

    qs = Company.objects.select_related("country", "owner")
    p = params
    if p.get("q"):
        q = p["q"].strip()
        qs = qs.filter(Q(name__icontains=q) | Q(domain__icontains=q) | Q(contacts__email__icontains=q) | Q(city__icontains=q)).distinct()
    if p.get("continent"):
        qs = qs.filter(country__continent_id=p["continent"])
    if p.get("country"):
        qs = qs.filter(country_id=p["country"])
    if p.get("product"):
        qs = qs.filter(products_interested__id=p["product"]).distinct()
    if p.get("business_type"):
        qs = qs.filter(business_type=p["business_type"])
    if p.get("oem_category"):
        qs = qs.filter(oem_category=p["oem_category"])
    if p.get("stage"):
        qs = qs.filter(stage=p["stage"])
    if p.get("owner"):
        qs = qs.filter(owner__isnull=True) if p["owner"] == "none" else qs.filter(owner_id=p["owner"])
    if p.get("source"):
        qs = qs.filter(lead_source=p["source"])
    if p.get("type"):
        qs = qs.filter(company_type=p["type"])
    if p.get("verification"):
        qs = qs.filter(verification_status=p["verification"])
    if p.get("followup_due") == "1":
        today = timezone.localdate()
        due_enrollment = Enrollment.objects.filter(company=OuterRef("pk"), status=Enrollment.Status.ACTIVE, next_due_at__date__lte=today)
        qs = qs.filter(Q(next_action_date__lte=today) | Exists(due_enrollment))
    if p.get("quotation"):
        if p["quotation"] == "none":
            qs = qs.filter(quotations__isnull=True)
        else:
            qs = qs.filter(quotations__status=p["quotation"]).distinct()
    if p.get("min_value"):
        try:
            qs = qs.filter(expected_order_value__gte=Decimal(p["min_value"]))
        except InvalidOperation:
            pass
    sort = p.get("sort") or "-updated_at"
    allowed = {"-updated_at", "name", "-oem_score", "-expected_order_value", "next_action_date", "-created_at"}
    return qs.order_by(sort if sort in allowed else "-updated_at")


def _filter_context(request):
    from apps.quotations.models import Quotation

    return {
        "continents": Continent.objects.all(),
        "countries": Country.objects.annotate(n=Count("companies")).filter(n__gt=0).order_by("name"),
        "products": ProductCategory.objects.all(),
        "business_types": BusinessType.choices,
        "oem_categories": OemCategory.choices,
        "stages": Stage.choices,
        "owners": User.objects.filter(is_active=True).order_by("username"),
        "sources": LeadSource.choices,
        "types": CompanyType.choices,
        "verification_choices": Company.VerificationStatus.choices,
        "quotation_statuses": Quotation.Status.choices,
        "f": request.GET,
    }


def company_list(request):
    qs = filtered_companies(request.GET).annotate(n_contacts=Count("contacts", distinct=True))
    if request.GET.get("export") == "xlsx":
        from apps.reports.exports import companies_to_xlsx

        return companies_to_xlsx(qs)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "crm/company_list.html", {"page_obj": page, **_filter_context(request)})


def company_detail(request, pk):
    company = get_object_or_404(Company.objects.select_related("country__continent", "owner", "research_run"), pk=pk)
    from apps.mailbox.models import InboundEmail
    from apps.outreach.models import EmailTemplate, OutboundEmail

    outbound = list(OutboundEmail.objects.filter(company=company).select_related("contact", "approved_by"))
    inbound = list(InboundEmail.objects.filter(company=company))
    timeline = sorted(
        [{"type": "out", "at": e.sent_at or e.created_at, "obj": e} for e in outbound]
        + [{"type": "in", "at": e.received_at, "obj": e} for e in inbound]
        + [{"type": "act", "at": a.occurred_at, "obj": a} for a in company.activities.exclude(kind__in=["email_out", "email_in"]).select_related("user", "contact")],
        key=lambda x: x["at"], reverse=True,
    )
    return render(request, "crm/company_detail.html", {
        "company": company,
        "contacts": company.contacts.all(),
        "sources": company.sources.all(),
        "timeline": timeline,
        "requirements": company.requirements.select_related("product_category"),
        "quotations": company.quotations.all(),
        "enrollments": company.enrollments.select_related("sequence", "contact"),
        "tasks": company.tasks.filter(done=False),
        "stage_history": company.stage_history.select_related("changed_by")[:20],
        "stages": Stage.choices,
        "templates": EmailTemplate.objects.filter(is_active=True),
        "activity_form": ActivityForm(company=company),
        "findings": company.findings.select_related("run")[:5],
    })


@editor_required
def company_edit(request, pk=None):
    company = get_object_or_404(Company, pk=pk) if pk else None
    form = CompanyForm(request.POST or None, instance=company)
    if request.method == "POST" and form.is_valid():
        if company is None:
            dup = services.find_duplicate(form.cleaned_data["name"], form.cleaned_data.get("website", ""),
                                          form.cleaned_data["country"].iso2 if form.cleaned_data.get("country") else "")
            if dup and not request.POST.get("allow_duplicate"):
                messages.warning(request, f"A matching company already exists: {dup.name}. Tick 'create anyway' to continue.")
                return render(request, "crm/company_form.html", {"form": form, "duplicate": dup})
        obj = form.save(commit=False)
        if company is None and not obj.owner_id:
            obj.owner = request.user
        obj.save()
        form.save_m2m()
        audit("crm.company_save", obj, user=request.user, summary=f"Company {obj} saved", changed=form.changed_data)
        messages.success(request, "Company saved.")
        return redirect(obj)
    return render(request, "crm/company_form.html", {"form": form, "company": company})


@editor_required
@require_POST
def company_stage(request, pk):
    company = get_object_or_404(Company, pk=pk)
    form = StageForm(request.POST)
    if form.is_valid():
        if services.set_stage(company, form.cleaned_data["stage"], user=request.user, reason=form.cleaned_data["reason"]):
            messages.success(request, f"Stage set to {company.get_stage_display()}.")
    return redirect(safe_next(request, company.get_absolute_url()))


@editor_required
@require_POST
def company_flag(request, pk):
    company = get_object_or_404(Company, pk=pk)
    action = request.POST.get("action")
    if action == "do_not_contact":
        company.do_not_contact = True
        company.save(update_fields=["do_not_contact", "updated_at"])
        from apps.followups.services import stop_company_enrollments

        stop_company_enrollments(company, "manual", note="Marked do-not-contact")
        messages.info(request, "Company marked do-not-contact; follow-ups stopped.")
    elif action == "allow_contact":
        company.do_not_contact = False
        company.save(update_fields=["do_not_contact", "updated_at"])
        messages.info(request, "Company can be contacted again.")
    audit("crm.company_flag", company, user=request.user, summary=f"{company}: {action}")
    return redirect(company)


@editor_required
@require_POST
def company_delete(request, pk):
    company = get_object_or_404(Company, pk=pk)
    if not is_manager(request.user):
        messages.error(request, "Only managers can delete companies.")
        return redirect(company)
    if company.quotations.exists():
        messages.error(request, "Companies with quotations cannot be deleted. Mark them as closed instead.")
        return redirect(company)
    name = company.name
    company.delete()
    audit("crm.company_delete", user=request.user, summary=f"Company {name} deleted")
    messages.success(request, f"{name} deleted.")
    return redirect("crm:company_list")


@editor_required
def contact_edit(request, company_pk=None, pk=None):
    contact = get_object_or_404(Contact, pk=pk) if pk else None
    company = contact.company if contact else get_object_or_404(Company, pk=company_pk)
    form = ContactForm(request.POST or None, instance=contact)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.company = company
        email_changed = "email" in form.changed_data
        if form.cleaned_data.get("confirm_email") and obj.email:
            obj.email_status = Contact.EmailStatus.MANUAL
            obj.email_checks = {**(obj.email_checks or {}), "confirmed_by": request.user.username,
                                "confirmed_at": timezone.now().isoformat()}
        elif email_changed:
            obj.email_status = Contact.EmailStatus.UNVERIFIED
            obj.email_checks = {}
        obj.save()
        if obj.is_primary:
            company.contacts.exclude(pk=obj.pk).update(is_primary=False)
        audit("crm.contact_save", obj, user=request.user, summary=f"Contact {obj} saved", changed=form.changed_data)
        if obj.email and obj.email_status == Contact.EmailStatus.UNVERIFIED:
            from apps.agents.registry import enqueue

            enqueue("research.verify_contact", {"contact_id": obj.pk}, user=request.user)
            messages.info(request, "E-mail verification queued.")
        messages.success(request, "Contact saved.")
        return redirect(company)
    return render(request, "core/form_page.html", {"form": form, "title": f"Contact at {company}",
                                                   "cancel_url": company.get_absolute_url()})


def contact_list(request):
    qs = Contact.objects.select_related("company__country")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(email__icontains=q) | Q(company__name__icontains=q) | Q(title__icontains=q))
    if request.GET.get("status"):
        qs = qs.filter(email_status=request.GET["status"])
    if request.GET.get("decision_makers") == "1":
        qs = qs.filter(is_decision_maker=True)
    page = Paginator(qs.order_by("company__name"), 50).get_page(request.GET.get("page"))
    return render(request, "crm/contact_list.html", {"page_obj": page, "statuses": Contact.EmailStatus.choices, "f": request.GET})


@editor_required
@require_POST
def activity_add(request, pk):
    company = get_object_or_404(Company, pk=pk)
    form = ActivityForm(request.POST, company=company)
    if form.is_valid():
        act = form.save(commit=False)
        act.company = company
        act.user = request.user
        act.occurred_at = act.occurred_at or timezone.now()
        act.save()
        if act.kind in (Activity.Kind.CALL, Activity.Kind.MEETING, Activity.Kind.WHATSAPP):
            services.touch_contacted(company, act.occurred_at)
        messages.success(request, "Activity logged.")
    else:
        messages.error(request, "Could not log the activity.")
    return redirect(company)


@editor_required
def requirement_edit(request, company_pk=None, pk=None):
    req = get_object_or_404(Requirement, pk=pk) if pk else None
    company = req.company if req else get_object_or_404(Company, pk=company_pk)
    form = RequirementForm(request.POST or None, instance=req, company=company)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.company = company
        obj.save()
        services.advance_stage(company, Stage.REQUIREMENT, user=request.user, reason="Requirement recorded")
        audit("crm.requirement_save", obj, user=request.user, summary=f"Requirement for {company}")
        messages.success(request, "Requirement saved.")
        return redirect(company)
    return render(request, "core/form_page.html", {"form": form, "title": f"Requirement – {company}",
                                                   "cancel_url": company.get_absolute_url()})


def pipeline(request):
    qs = filtered_companies(request.GET).exclude(stage=Stage.LOST)
    columns = []
    for value in STAGE_ORDER:
        stage_qs = qs.filter(stage=value)
        columns.append({"value": value, "label": Stage(value).label, "count": stage_qs.count(),
                        "companies": stage_qs.order_by("-oem_score", "-updated_at")[:25]})
    lost = filtered_companies(request.GET).filter(stage=Stage.LOST).count()
    return render(request, "crm/pipeline.html", {"columns": columns, "lost": lost, "stages": Stage.choices, **_filter_context(request)})


def task_list(request):
    scope = request.GET.get("scope", "mine")
    qs = Task.objects.select_related("company", "assigned_to")
    if scope == "mine":
        qs = qs.filter(assigned_to=request.user)
    elif scope == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)
    if request.GET.get("show") != "done":
        qs = qs.filter(done=False)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "crm/task_list.html", {"page_obj": page, "scope": scope, "show": request.GET.get("show", ""),
                                                  "form": TaskForm(initial={"assigned_to": request.user})})


@editor_required
@require_POST
def task_create(request):
    form = TaskForm(request.POST)
    if form.is_valid():
        t = form.save(commit=False)
        t.created_by = request.user
        t.save()
        messages.success(request, "Task created.")
    else:
        messages.error(request, "Task not created: " + "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect(safe_next(request, "crm:task_list"))


@editor_required
@require_POST
def task_done(request, pk):
    t = get_object_or_404(Task, pk=pk)
    t.done = not t.done
    t.done_at = timezone.now() if t.done else None
    t.save(update_fields=["done", "done_at"])
    return redirect(safe_next(request, "crm:task_list"))


def _resolve_country(value: str):
    value = (value or "").strip()
    if not value:
        return None
    if len(value) == 2:
        c = Country.objects.filter(iso2=value.upper()).first()
        if c:
            return c
    return Country.objects.filter(name__iexact=value).first()


@editor_required
def company_import(request):
    form = CompanyImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            text = form.cleaned_data["file"].read().decode("utf-8-sig")
        except UnicodeDecodeError:
            messages.error(request, "The file must be UTF-8 encoded.")
            return redirect("crm:company_import")
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(io.StringIO(text))]
        created, merged, errors, verify_ids = 0, 0, [], []
        valid_types = {c for c, _ in CompanyType.choices}
        with transaction.atomic():
            for i, row in enumerate(rows, start=2):
                name = row.get("name")
                if not name:
                    errors.append(f"Row {i}: name is required")
                    continue
                country = _resolve_country(row.get("country", ""))
                if row.get("country") and not country:
                    errors.append(f"Row {i}: unknown country '{row.get('country')}' (imported without country)")
                email = row.get("email", "").lower()
                company = services.find_duplicate(name, row.get("website", ""), country.iso2 if country else "", [email])
                if company:
                    merged += 1
                else:
                    website = row.get("website", "")
                    if website and "://" not in website:
                        website = "https://" + website
                    company = Company.objects.create(
                        name=name, website=website, country=country, city=row.get("city", ""),
                        company_type=row.get("company_type") if row.get("company_type") in valid_types else CompanyType.OTHER,
                        lead_source=form.cleaned_data["lead_source"], notes=row.get("notes", ""), owner=request.user,
                        is_existing_customer=form.cleaned_data["lead_source"] == "existing",
                    )
                    created += 1
                if row.get("source_url"):
                    company.sources.get_or_create(url=row["source_url"], defaults={"kind": "other", "supports": "Imported list"})
                if email or row.get("contact_name"):
                    contact = company.contacts.filter(email__iexact=email).first() if email else None
                    if contact is None:
                        contact = Contact(company=company, email=email)
                    contact.full_name = contact.full_name or row.get("contact_name", "")
                    contact.title = contact.title or row.get("contact_title", "")
                    contact.phone = contact.phone or row.get("phone", "")
                    if email and form.cleaned_data["emails_confirmed"]:
                        contact.email_status = Contact.EmailStatus.MANUAL
                        contact.email_checks = {"confirmed_by": request.user.username, "confirmed_via": "import",
                                                "confirmed_at": timezone.now().isoformat()}
                    contact.save()
                verify_ids.append(company.pk)
        audit("crm.import", user=request.user, summary=f"{created} created, {merged} matched existing", errors=errors[:50])
        if form.cleaned_data["run_verification"] and verify_ids:
            from apps.agents.registry import enqueue

            enqueue("research.verify_companies", {"company_ids": sorted(set(verify_ids))}, user=request.user)
            messages.info(request, "Verification job queued for imported companies.")
        messages.success(request, f"Import finished: {created} new companies, {merged} matched existing records, {len(errors)} warnings.")
        for e in errors[:10]:
            messages.warning(request, e)
        return redirect("crm:company_list")
    return render(request, "core/form_page.html", {"form": form, "multipart": True, "title": "Import companies (CSV)",
                                                   "submit_label": "Import"})


@editor_required
@require_POST
def contact_delete(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    company = contact.company
    contact.delete()
    audit("crm.contact_delete", company, user=request.user, summary=f"Contact {pk} deleted")
    messages.success(request, "Contact deleted.")
    return redirect(company)


def export_contacts_csv(request):
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="contacts.csv"'
    w = csv.writer(resp)
    w.writerow(["company", "country", "name", "title", "email", "email_status", "phone", "linkedin", "source"])
    for c in Contact.objects.select_related("company__country"):
        w.writerow([c.company.name, c.company.country.name if c.company.country else "", c.full_name, c.title, c.email,
                    c.get_email_status_display(), c.phone, c.linkedin_url, c.email_source_url])
    return resp
