from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.permissions import editor_required
from apps.crm.models import Company, Requirement

from . import services
from .forms import DiscountForm, LineForm, PriceOverrideForm, QuotationCreateForm, QuotationTermsForm
from .models import Quotation, QuotationLine
from .pdf import render_quotation_pdf


def quotation_list(request):
    qs = Quotation.objects.select_related("company__country", "approved_by")
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    return render(request, "quotations/list.html", {"page_obj": page, "statuses": Quotation.Status.choices, "f": request.GET})


@editor_required
def create_for_company(request, pk):
    company = get_object_or_404(Company, pk=pk)
    req = Requirement.objects.filter(pk=request.GET.get("requirement") or request.POST.get("requirement"), company=company).first()
    form = QuotationCreateForm(request.POST or None, company=company, initial={"contact": company.primary_contact})
    if request.method == "POST" and form.is_valid():
        q = services.create_quotation(company, contact=form.cleaned_data["contact"], currency=form.cleaned_data["currency"],
                                      requirements=[req] if req else [], user=request.user)
        messages.success(request, f"Quotation {q.number} created. Prices are filled only from approved price lists.")
        return redirect("quotations:detail", pk=q.pk)
    return render(request, "quotations/create.html", {"form": form, "company": company, "requirement": req})


def detail(request, pk):
    q = get_object_or_404(Quotation.objects.select_related("company", "contact", "approved_by", "source_email", "discount_set_by"), pk=pk)
    if not q.is_locked:
        services.refresh_status(q)
    return render(request, "quotations/detail.html", {
        "q": q, "lines": q.lines.select_related("variant__product", "price_item__price_list", "price_overridden_by"),
        "terms_form": QuotationTermsForm(instance=q), "line_form": LineForm(), "emails": q.emails.all(),
    })


@editor_required
@require_POST
def update_terms(request, pk):
    q = get_object_or_404(Quotation, pk=pk)
    if q.is_locked:
        messages.error(request, "Approved quotations cannot be edited. Create a revision.")
        return redirect("quotations:detail", pk=pk)
    old_currency = q.currency
    form = QuotationTermsForm(request.POST, instance=q)
    if form.is_valid():
        q = form.save()
        if q.status == Quotation.Status.PENDING_APPROVAL:
            q.status = Quotation.Status.DRAFT
            q.save(update_fields=["status"])
        if q.currency != old_currency:
            services.reprice(q)
        services.refresh_status(q)
        audit("quotation.terms", q, user=request.user, summary=f"{q.number} terms updated", changed=form.changed_data)
        messages.success(request, "Terms saved.")
    else:
        messages.error(request, "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))
    return redirect("quotations:detail", pk=pk)


@editor_required
@require_POST
def line_add(request, pk):
    q = get_object_or_404(Quotation, pk=pk)
    if q.is_locked:
        messages.error(request, "Approved quotations cannot be edited.")
        return redirect("quotations:detail", pk=pk)
    form = LineForm(request.POST)
    if form.is_valid():
        line = form.save(commit=False)
        line.quotation = q
        line.position = (q.lines.count() or 0) + 1
        services.price_line(line, q.currency)
        line.save()
        if q.status == Quotation.Status.PENDING_APPROVAL:
            q.status = Quotation.Status.DRAFT
            q.save(update_fields=["status"])
        services.refresh_status(q)
        if line.unit_price is None:
            messages.warning(request, f"No approved {q.currency} price for this size - a manager must price it.")
    else:
        messages.error(request, "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))
    return redirect("quotations:detail", pk=pk)


@editor_required
@require_POST
def line_action(request, pk):
    line = get_object_or_404(QuotationLine, pk=pk)
    q = line.quotation
    if q.is_locked:
        messages.error(request, "Approved quotations cannot be edited.")
        return redirect("quotations:detail", pk=q.pk)
    action = request.POST.get("action")
    try:
        if action == "delete":
            line.delete()
            for i, other in enumerate(q.lines.order_by("position", "id"), start=1):
                if other.position != i:
                    other.position = i
                    other.save(update_fields=["position"])
        elif action == "price":
            form = PriceOverrideForm(request.POST)
            if form.is_valid():
                services.override_price(line, form.cleaned_data["unit_price"], request.user)
                messages.warning(request, "Manual price set by a manager - it is not from the approved price list.")
            else:
                messages.error(request, "Invalid price.")
        elif action == "qty":
            qty = int(request.POST.get("quantity") or 0)
            if qty > 0:
                line.quantity = qty
                services.price_line(line, q.currency)
                line.save()
        elif action == "match":
            from apps.products.models import ProductVariant

            v = ProductVariant.objects.filter(pk=request.POST.get("variant"), product__is_verified=True).first()
            if v:
                line.variant = v
                line.description = ""
                services.price_line(line, q.currency)
                line.save()
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    if q.status == Quotation.Status.PENDING_APPROVAL:
        q.status = Quotation.Status.DRAFT
        q.save(update_fields=["status"])
    services.refresh_status(q)
    return redirect("quotations:detail", pk=q.pk)


@editor_required
@require_POST
def quotation_action(request, pk):
    q = get_object_or_404(Quotation, pk=pk)
    action = request.POST.get("action")
    try:
        if action == "reprice":
            services.reprice(q)
            messages.info(request, "Prices refreshed from approved price lists.")
        elif action == "submit":
            services.submit_for_approval(q, request.user)
            messages.success(request, "Submitted for manager approval.")
        elif action == "approve":
            services.approve(q, request.user)
            messages.success(request, f"{q.number} approved and PDF generated.")
        elif action == "discount":
            form = DiscountForm(request.POST)
            if form.is_valid():
                services.set_discount(q, form.cleaned_data["discount_percent"], request.user)
                messages.success(request, "Discount applied.")
        elif action == "send":
            email = services.send_quotation(q, request.user)
            messages.success(request, "Quotation e-mail prepared. Review and approve it to send.")
            return redirect("outreach:email_detail", pk=email.pk)
        elif action == "revision":
            new = services.new_revision(q, request.user)
            messages.success(request, f"Revision {new.number} created.")
            return redirect("quotations:detail", pk=new.pk)
        elif action in ("accepted", "rejected"):
            services.mark_outcome(q, action == "accepted", request.user)
        elif action == "cancel" and not q.is_locked:
            q.status = Quotation.Status.CANCELLED
            q.save(update_fields=["status"])
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("quotations:detail", pk=pk)


def pdf(request, pk):
    q = get_object_or_404(Quotation, pk=pk)
    if q.pdf and q.is_locked:
        q.pdf.open("rb")
        data = q.pdf.read()
        q.pdf.close()
    else:
        data = render_quotation_pdf(q, draft=True)
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{q.number.replace("/", "-")}.pdf"'
    return resp
