import secrets

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.agents.llm import ai_configured, not_configured_message
from apps.agents.registry import enqueue
from apps.core.audit import audit
from apps.core.forms import BootstrapFormMixin
from apps.core.permissions import admin_required, editor_required

from .models import Classification, InboundEmail, MailAccount, Provider
from .providers.base import ProviderError, get_provider


def account_list(request):
    providers = []
    for value, label in Provider.choices:
        p = get_provider(value)
        providers.append({"value": value, "label": label, "configured": p.is_configured(), "redirect_uri": p.redirect_uri()})
    return render(request, "mailbox/account_list.html", {"accounts": MailAccount.objects.select_related("owner"), "providers": providers})


@admin_required
def oauth_start(request, provider):
    p = get_provider(provider)
    if not p.is_configured():
        messages.error(request, f"{p.label} OAuth client is not configured on the server (see docs/EMAIL_SETUP.md).")
        return redirect("mailbox:account_list")
    state = secrets.token_urlsafe(24)
    request.session[f"oauth_state_{provider}"] = state
    return redirect(p.authorize_url(state))


@admin_required
def oauth_callback(request, provider):
    p = get_provider(provider)
    expected = request.session.pop(f"oauth_state_{provider}", None)
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "OAuth state mismatch - please try connecting again.")
        return redirect("mailbox:account_list")
    if request.GET.get("error"):
        messages.error(request, f"{p.label} authorisation was refused: {request.GET.get('error_description') or request.GET['error']}")
        return redirect("mailbox:account_list")
    code = request.GET.get("code")
    if not code:
        messages.error(request, "No authorisation code received.")
        return redirect("mailbox:account_list")
    try:
        tokens = p.exchange_code(code)
        ident = p.identity(tokens)
    except ProviderError as exc:
        messages.error(request, f"Connection failed: {exc}")
        return redirect("mailbox:account_list")
    account, created = MailAccount.objects.get_or_create(provider=provider, email_address=ident.email,
                                                          defaults={"owner": request.user})
    account.access_token = tokens.access_token
    if tokens.refresh_token:
        account.refresh_token = tokens.refresh_token
    account.token_expires_at = timezone.now() + timezone.timedelta(seconds=tokens.expires_in or 3600)
    account.scopes = tokens.scope
    account.provider_account_id = ident.account_id or account.provider_account_id
    account.provider_data = {**account.provider_data, **ident.extra, **tokens.extra}
    account.display_name = account.display_name or ident.display_name
    account.status = MailAccount.Status.CONNECTED
    account.is_active = True
    account.last_error = ""
    if not MailAccount.objects.filter(is_default_sender=True).exclude(pk=account.pk).exists():
        account.is_default_sender = True
    account.save()
    audit("mailbox.connected", account, user=request.user, summary=f"{account} connected")
    messages.success(request, f"{account.email_address} connected.")
    return redirect("mailbox:account_list")


class AccountForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = MailAccount
        fields = ["display_name", "owner", "is_active", "use_for_sending", "is_default_sender", "daily_limit"]


@admin_required
def account_edit(request, pk):
    account = get_object_or_404(MailAccount, pk=pk)
    form = AccountForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        if obj.is_default_sender:
            MailAccount.objects.exclude(pk=obj.pk).update(is_default_sender=False)
        audit("mailbox.updated", obj, user=request.user, summary=f"{obj} settings updated")
        messages.success(request, "Mailbox settings saved.")
        return redirect("mailbox:account_list")
    return render(request, "core/form_page.html", {"form": form, "title": f"Mailbox {account.email_address}"})


@admin_required
@require_POST
def account_disconnect(request, pk):
    account = get_object_or_404(MailAccount, pk=pk)
    account.access_token = ""
    account.refresh_token = ""
    account.status = MailAccount.Status.REVOKED
    account.is_active = False
    account.save()
    audit("mailbox.disconnected", account, user=request.user, summary=f"{account} disconnected; tokens deleted")
    messages.info(request, "Mailbox disconnected and its tokens deleted. Also revoke access in the provider's security settings if needed.")
    return redirect("mailbox:account_list")


@editor_required
@require_POST
def sync_now(request):
    enqueue("mailbox.sync_all", {}, user=request.user)
    messages.success(request, "Mailbox sync queued.")
    return redirect("mailbox:inbox")


def inbox(request):
    qs = InboundEmail.objects.select_related("company", "contact", "account")
    view = request.GET.get("view", "attention")
    if view == "attention":
        qs = qs.filter(needs_attention=True)
    if request.GET.get("cls"):
        qs = qs.filter(classification=request.GET["cls"])
    if request.GET.get("q"):
        q = request.GET["q"]
        qs = qs.filter(Q(subject__icontains=q) | Q(from_email__icontains=q) | Q(company__name__icontains=q))
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    return render(request, "mailbox/inbox.html", {"page_obj": page, "view": view, "classes": Classification.choices, "f": request.GET})


def inbound_detail(request, pk):
    inbound = get_object_or_404(InboundEmail.objects.select_related("company", "contact", "account", "matched_outbound"), pk=pk)
    return render(request, "mailbox/inbound_detail.html", {
        "inbound": inbound, "classes": Classification.choices, "drafts": inbound.draft_replies.all(),
        "requirements": inbound.requirements.all(), "quotations": inbound.quotations.all(), "ai": ai_configured("drafting"),
    })


@editor_required
@require_POST
def inbound_action(request, pk):
    inbound = get_object_or_404(InboundEmail, pk=pk)
    action = request.POST.get("action")
    if action == "handled":
        inbound.needs_attention = False
        inbound.handled_by = request.user
        inbound.save(update_fields=["needs_attention", "handled_by"])
        messages.success(request, "Marked as handled.")
        return redirect("mailbox:inbox")
    if action == "reclassify":
        cls = request.POST.get("classification")
        if cls in Classification.values:
            old = inbound.classification
            inbound.classification = cls
            inbound.classified_by = "human"
            inbound.save(update_fields=["classification", "classified_by"])
            if inbound.company_id:
                from .processing import apply_to_crm

                if cls not in (Classification.AUTO_REPLY, Classification.UNRELATED, Classification.BOUNCE):
                    if cls == Classification.UNSUBSCRIBE:
                        from .processing import handle_unsubscribe

                        handle_unsubscribe(inbound)
                    else:
                        apply_to_crm(inbound, {**(inbound.extracted or {}), "reply_needed": False})
            audit("email.reclassified", inbound, user=request.user, summary=f"{old} → {cls}")
            messages.success(request, "Classification updated and CRM adjusted.")
    elif action == "draft_reply":
        if not ai_configured("drafting"):
            messages.error(request, not_configured_message("drafting"))
        elif inbound.company_id is None:
            messages.error(request, "Link the e-mail to a company first.")
        else:
            enqueue("mailbox.draft_reply", {"inbound_id": inbound.pk, "user_id": request.user.pk}, user=request.user)
            messages.success(request, "Reply draft requested - it will appear in the approval queue.")
    elif action == "reprocess":
        enqueue("mailbox.process_one", {"inbound_id": inbound.pk, "force": True}, user=request.user)
        messages.success(request, "Re-processing queued.")
    elif action == "link_company":
        from apps.crm.models import Company

        company = Company.objects.filter(pk=request.POST.get("company_id")).first()
        if company:
            inbound.company = company
            inbound.save(update_fields=["company"])
            messages.success(request, f"Linked to {company.name}.")
    return redirect("mailbox:inbound_detail", pk=pk)
