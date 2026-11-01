from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core import signing
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.agents.llm import ai_configured
from apps.agents.registry import enqueue
from apps.core import policy
from apps.core.audit import audit
from apps.core.forms import BootstrapFormMixin
from apps.core.models import AutomationState, CompanyProfile, SystemSettings
from apps.core.permissions import editor_required, is_manager, manager_required
from apps.crm.models import Company, Contact
from apps.mailbox.models import InboundEmail, MailAccount
from apps.products.models import CompanyDocument

from .drafting import signature
from .models import SENSITIVE_TEMPLATE_KINDS, EmailKind, EmailTemplate, OutboundEmail, SuppressionEntry
from .sending import read_unsubscribe_token


class EmailEditForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = OutboundEmail
        fields = ["to_email", "cc_emails", "account", "subject", "body_text", "attachments"]
        widgets = {"body_text": forms.Textarea(attrs={"rows": 14}), "attachments": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["attachments"].queryset = CompanyDocument.objects.filter(approved_for_sending=True)
        self.fields["attachments"].required = False
        self.fields["account"].queryset = MailAccount.objects.filter(is_active=True, use_for_sending=True)
        self.fields["account"].required = False
        self.fields["account"].help_text = "Leave empty to use the default sending mailbox."


def approval_queue(request):
    qs = OutboundEmail.objects.filter(status=OutboundEmail.Status.PENDING_APPROVAL).select_related("company__country", "contact", "template")
    if request.GET.get("kind"):
        qs = qs.filter(kind=request.GET["kind"])
    if request.GET.get("sensitive") == "1":
        qs = qs.exclude(sensitive_flags=[])
    page = Paginator(qs.order_by("created_at"), 20).get_page(request.GET.get("page"))
    blocked = OutboundEmail.objects.filter(status=OutboundEmail.Status.BLOCKED, created_at__gte=timezone.now() - timezone.timedelta(days=7)).count()
    return render(request, "outreach/approval_queue.html", {"page_obj": page, "kinds": EmailKind.choices, "f": request.GET,
                                                            "blocked": blocked, "labels": policy.SENSITIVE_LABELS})


def email_list(request):
    qs = OutboundEmail.objects.select_related("company", "contact", "account", "approved_by")
    for key in ("status", "kind"):
        if request.GET.get(key):
            qs = qs.filter(**{key: request.GET[key]})
    if request.GET.get("q"):
        q = request.GET["q"]
        qs = qs.filter(Q(subject__icontains=q) | Q(to_email__icontains=q) | Q(company__name__icontains=q))
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    return render(request, "outreach/email_list.html", {"page_obj": page, "statuses": OutboundEmail.Status.choices,
                                                        "kinds": EmailKind.choices, "f": request.GET})


def email_detail(request, pk):
    email = get_object_or_404(OutboundEmail.objects.select_related("company", "contact", "account", "template", "approved_by",
                                                                   "in_reply_to", "quotation"), pk=pk)
    decision = policy.evaluate(email) if email.is_open else None
    sys = SystemSettings.get_solo()
    footer = sys.unsubscribe_footer.format(unsubscribe_url="[personal unsubscribe link]") if email.kind in ("initial", "followup") else ""
    return render(request, "outreach/email_detail.html", {
        "email": email, "decision": decision, "footer": footer, "labels": policy.SENSITIVE_LABELS,
        "replies": email.replies.all(), "can_approve_sensitive": is_manager(request.user),
        "automation_stopped": sys.automation_state == AutomationState.STOPPED,
    })


@editor_required
def email_edit(request, pk):
    email = get_object_or_404(OutboundEmail, pk=pk)
    if not email.is_open and email.status != OutboundEmail.Status.BLOCKED:
        messages.error(request, "Only unsent e-mails can be edited.")
        return redirect("outreach:email_detail", pk=pk)
    form = EmailEditForm(request.POST or None, instance=email)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.status = OutboundEmail.Status.DRAFT
        obj.approved_by = None
        obj.approved_at = None
        obj.save()
        form.save_m2m()
        policy.apply(obj)
        audit("email.edited", obj, user=request.user, summary=f"E-mail #{obj.pk} edited", changed=form.changed_data)
        messages.success(request, "Changes saved. The e-mail was re-checked against the sending policy.")
        return redirect("outreach:email_detail", pk=pk)
    return render(request, "outreach/email_edit.html", {"form": form, "email": email})


@editor_required
@require_POST
def email_decide(request, pk):
    email = get_object_or_404(OutboundEmail, pk=pk)
    action = request.POST.get("action")
    if action == "approve":
        if email.status not in (OutboundEmail.Status.PENDING_APPROVAL, OutboundEmail.Status.DRAFT):
            messages.error(request, "This e-mail is not waiting for approval.")
            return redirect("outreach:email_detail", pk=pk)
        decision = policy.evaluate(email)
        if decision.is_blocked:
            policy.apply(email)
            messages.error(request, "Blocked by policy: " + " ".join(decision.blocked))
            return redirect("outreach:email_detail", pk=pk)
        commercial = email.kind == EmailKind.QUOTATION or (email.template and email.template.kind in SENSITIVE_TEMPLATE_KINDS)
        if (decision.sensitive or commercial) and not is_manager(request.user):
            messages.error(request, "This e-mail contains commercial/technical/legal content and must be approved by a manager.")
            return redirect("outreach:email_detail", pk=pk)
        email.status = OutboundEmail.Status.APPROVED
        email.approved_by = request.user
        email.approved_at = timezone.now()
        email.error = ""
        email.save(update_fields=["status", "approved_by", "approved_at", "error", "updated_at"])
        audit("email.approved", email, user=request.user, summary=f"Approved: {email.subject}", sensitive=decision.sensitive)
        enqueue("outreach.send_one", {"email_id": email.pk}, user=request.user)
        messages.success(request, "Approved. It will be sent in the recipient's business hours, within the sending limits.")
        nxt = OutboundEmail.objects.filter(status=OutboundEmail.Status.PENDING_APPROVAL).order_by("created_at").first()
        if request.POST.get("next") == "queue" and nxt:
            return redirect("outreach:email_detail", pk=nxt.pk)
        return redirect("outreach:approval_queue" if request.POST.get("next") == "queue" else "outreach:email_detail",
                        **({} if request.POST.get("next") == "queue" else {"pk": pk}))
    if action in ("reject", "cancel"):
        if not email.is_open and email.status != OutboundEmail.Status.BLOCKED:
            messages.error(request, "This e-mail can no longer be changed.")
            return redirect("outreach:email_detail", pk=pk)
        email.status = OutboundEmail.Status.REJECTED if action == "reject" else OutboundEmail.Status.CANCELLED
        email.rejected_reason = (request.POST.get("reason") or "")[:500]
        email.save(update_fields=["status", "rejected_reason", "updated_at"])
        if email.kind == EmailKind.FOLLOWUP and email.enrollment_id:
            from apps.followups.services import on_followup_discarded

            on_followup_discarded(email)
        audit(f"email.{action}ed", email, user=request.user, summary=email.rejected_reason or email.subject)
        messages.info(request, "E-mail rejected." if action == "reject" else "E-mail cancelled.")
        return redirect("outreach:approval_queue")
    if action == "recheck":
        policy.apply(email)
        messages.info(request, "Policy re-checked.")
    return redirect("outreach:email_detail", pk=pk)


@editor_required
@require_POST
def draft_for_company(request, pk):
    company = get_object_or_404(Company, pk=pk)
    if not ai_configured():
        messages.error(request, "AI is not configured (ANTHROPIC_API_KEY).")
        return redirect(company)
    contact = company.contacts.filter(pk=request.POST.get("contact")).first()
    if contact is None or not contact.is_emailable:
        messages.error(request, "Choose a contact with a verified e-mail address.")
        return redirect(company)
    missing = CompanyProfile.get_solo().missing_for_outreach()
    if missing:
        messages.error(request, "Complete the company profile first (Settings → Company profile): " + ", ".join(missing))
        return redirect(company)
    enqueue("outreach.draft_for_company", {
        "company_id": company.pk, "contact_id": contact.pk, "template_id": request.POST.get("template") or None,
        "instructions": request.POST.get("instructions", "")[:1000], "user_id": request.user.pk,
    }, user=request.user, max_attempts=1)
    messages.success(request, "Draft requested. It will appear in the approval queue in a minute.")
    return redirect(company)


class ComposeForm(BootstrapFormMixin, forms.Form):
    contact = forms.ModelChoiceField(queryset=Contact.objects.none(), required=False)
    to_email = forms.EmailField(required=False, help_text="Used when replying to an inbound e-mail.")
    subject = forms.CharField(max_length=300)
    body_text = forms.CharField(widget=forms.Textarea(attrs={"rows": 14}))
    attachments = forms.ModelMultipleChoiceField(queryset=CompanyDocument.objects.filter(approved_for_sending=True),
                                                 required=False, widget=forms.CheckboxSelectMultiple)


@editor_required
def compose(request):
    inbound = InboundEmail.objects.filter(pk=request.GET.get("inbound") or request.POST.get("inbound")).first()
    company = Company.objects.filter(pk=request.GET.get("company") or request.POST.get("company")).first()
    if inbound and inbound.company_id:
        company = inbound.company
    if company is None:
        messages.error(request, "Open a company or an inbound e-mail first, then write the e-mail from there.")
        return redirect("crm:company_list")
    initial = {}
    if inbound:
        initial = {"to_email": inbound.from_email, "subject": inbound.subject if inbound.subject.lower().startswith("re:") else f"Re: {inbound.subject}",
                   "body_text": f"Dear {inbound.from_name or 'Sir/Madam'},\n\n\n\nBest regards,\n\n{signature(request.user)}"}
    else:
        initial = {"body_text": f"\n\nBest regards,\n\n{signature(request.user)}"}
    form = ComposeForm(request.POST or None, initial=initial)
    form.fields["contact"].queryset = company.contacts.exclude(email="")
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        to = (d["to_email"] or (d["contact"].email if d["contact"] else "")).lower()
        if not to:
            form.add_error("to_email", "Choose a contact or enter the recipient.")
        else:
            email = OutboundEmail.objects.create(
                company=company, contact=d["contact"] or (inbound.contact if inbound else None), to_email=to,
                kind=EmailKind.REPLY if inbound else EmailKind.MANUAL, subject=d["subject"], body_text=d["body_text"],
                in_reply_to=inbound, account=inbound.account if inbound else None, created_by=request.user,
            )
            email.attachments.set(d["attachments"])
            policy.apply(email)
            audit("email.composed", email, user=request.user, summary=email.subject)
            messages.success(request, "E-mail saved. Review it and approve to send.")
            return redirect("outreach:email_detail", pk=email.pk)
    return render(request, "outreach/compose.html", {"form": form, "company": company, "inbound": inbound})


class TemplateForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = EmailTemplate
        fields = ["name", "purpose", "guidance", "example_subject", "call_to_action", "max_words", "approved_for_auto_send", "is_active"]
        widgets = {"purpose": forms.Textarea(attrs={"rows": 2}), "guidance": forms.Textarea(attrs={"rows": 6})}

    def clean_approved_for_auto_send(self):
        value = self.cleaned_data["approved_for_auto_send"]
        if value and self.instance.kind in SENSITIVE_TEMPLATE_KINDS:
            raise forms.ValidationError("Quotation and negotiation e-mails can never be sent automatically.")
        return value


def template_list(request):
    return render(request, "outreach/template_list.html", {"templates": EmailTemplate.objects.all()})


@manager_required
def template_edit(request, pk):
    t = get_object_or_404(EmailTemplate, pk=pk)
    form = TemplateForm(request.POST or None, instance=t)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit("template.saved", t, user=request.user, summary=f"Template {t.name} saved", changed=form.changed_data)
        messages.success(request, "Template saved.")
        return redirect("outreach:template_list")
    return render(request, "core/form_page.html", {"form": form, "title": f"Template: {t.get_kind_display()}",
                                                   "subtitle": "Guidance for the outreach agent. The AI still personalises every e-mail."})


class SuppressionForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SuppressionEntry
        fields = ["email", "domain", "reason", "note"]

    def clean(self):
        data = super().clean()
        if not data.get("email") and not data.get("domain"):
            raise forms.ValidationError("Enter an e-mail address or a domain.")
        return data


def suppression_list(request):
    qs = SuppressionEntry.objects.select_related("created_by")
    if request.GET.get("q"):
        qs = qs.filter(Q(email__icontains=request.GET["q"]) | Q(domain__icontains=request.GET["q"]))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "outreach/suppression_list.html", {"page_obj": page, "form": SuppressionForm(), "q": request.GET.get("q", "")})


@editor_required
@require_POST
def suppression_add(request):
    form = SuppressionForm(request.POST)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.email = obj.email.lower()
        obj.domain = obj.domain.lower().lstrip("@")
        obj.created_by = request.user
        obj.save()
        audit("suppression.add", obj, user=request.user, summary=str(obj))
        messages.success(request, "Added to the suppression list.")
    else:
        messages.error(request, "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect("outreach:suppression_list")


@manager_required
@require_POST
def suppression_remove(request, pk):
    obj = get_object_or_404(SuppressionEntry, pk=pk)
    if obj.reason in (SuppressionEntry.Reason.UNSUBSCRIBE, SuppressionEntry.Reason.COMPLAINT):
        messages.error(request, "Unsubscribe requests and complaints cannot be removed from the suppression list.")
        return redirect("outreach:suppression_list")
    audit("suppression.remove", obj, user=request.user, summary=str(obj))
    obj.delete()
    messages.success(request, "Removed from the suppression list.")
    return redirect("outreach:suppression_list")


@login_not_required
@csrf_exempt
def unsubscribe(request, token):
    """Public unsubscribe page (also handles RFC 8058 one-click POSTs)."""
    try:
        data = read_unsubscribe_token(token)
    except signing.BadSignature:
        return render(request, "outreach/unsubscribe.html", {"invalid": True}, status=400)
    email = data["e"]
    if request.method == "POST":
        SuppressionEntry.add(email, SuppressionEntry.Reason.UNSUBSCRIBE, note="Unsubscribe link")
        from apps.followups.services import stop_contact_enrollments

        for contact in Contact.objects.filter(email__iexact=email):
            contact.unsubscribed_at = contact.unsubscribed_at or timezone.now()
            contact.do_not_contact = True
            contact.save(update_fields=["unsubscribed_at", "do_not_contact", "updated_at"])
            stop_contact_enrollments(contact, "unsubscribed", note="Unsubscribe link")
        OutboundEmail.objects.filter(to_email__iexact=email, status__in=[
            OutboundEmail.Status.DRAFT, OutboundEmail.Status.PENDING_APPROVAL, OutboundEmail.Status.APPROVED,
        ]).update(status=OutboundEmail.Status.CANCELLED, rejected_reason="Recipient unsubscribed")
        audit("email.unsubscribe", agent="system", summary=f"{email} unsubscribed via link")
        return render(request, "outreach/unsubscribe.html", {"done": True, "email": email})
    return render(request, "outreach/unsubscribe.html", {"email": email, "token": token})
