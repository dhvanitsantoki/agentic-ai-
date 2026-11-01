from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.forms import inlineformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.forms import BootstrapFormMixin
from apps.core.permissions import editor_required, manager_required, safe_next

from .models import Enrollment, FollowUpSequence, FollowUpStep
from .services import cancel_open_followups, schedule_next


def enrollment_list(request):
    status = request.GET.get("status", "active")
    qs = Enrollment.objects.select_related("company__country", "contact", "sequence")
    if status:
        qs = qs.filter(status=status)
    if request.GET.get("due") == "1":
        qs = qs.filter(next_due_at__lte=timezone.now())
    page = Paginator(qs.order_by("next_due_at", "-started_at"), 50).get_page(request.GET.get("page"))
    return render(request, "followups/enrollment_list.html", {
        "page_obj": page, "status": status, "statuses": Enrollment.Status.choices, "due": request.GET.get("due", ""),
        "sequences": FollowUpSequence.objects.prefetch_related("steps"),
    })


@editor_required
@require_POST
def enrollment_action(request, pk):
    en = get_object_or_404(Enrollment, pk=pk)
    action = request.POST.get("action")
    if action == "stop" and en.stop("manual", f"Stopped by {request.user.username}"):
        cancel_open_followups(en)
        messages.info(request, "Follow-ups stopped.")
    elif action == "pause" and en.status == Enrollment.Status.ACTIVE:
        en.status = Enrollment.Status.PAUSED
        en.log("paused", by=request.user.username)
        en.save(update_fields=["status", "history"])
        messages.info(request, "Follow-ups paused.")
    elif action == "resume" and en.status == Enrollment.Status.PAUSED:
        en.status = Enrollment.Status.ACTIVE
        en.log("resumed", by=request.user.username)
        en.save(update_fields=["status", "history"])
        if en.next_due_at is None:
            schedule_next(en)
        messages.success(request, "Follow-ups resumed.")
    audit(f"followup.{action}", en, user=request.user, summary=str(en))
    return redirect(safe_next(request, "followups:enrollment_list"))


class SequenceForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = FollowUpSequence
        fields = ["name", "description", "is_default", "is_active"]


class StepForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = FollowUpStep
        fields = ["step_number", "day_offset", "angle", "is_final"]
        widgets = {"angle": forms.TextInput()}


StepFormSet = inlineformset_factory(FollowUpSequence, FollowUpStep, form=StepForm, extra=1, can_delete=True)


@manager_required
def sequence_edit(request, pk=None):
    seq = get_object_or_404(FollowUpSequence, pk=pk) if pk else FollowUpSequence()
    form = SequenceForm(request.POST or None, instance=seq)
    formset = StepFormSet(request.POST or None, instance=seq)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        offsets = [f.cleaned_data["day_offset"] for f in formset.forms if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        if offsets != sorted(offsets) or len(set(offsets)) != len(offsets):
            messages.error(request, "Day offsets must increase from step to step.")
        elif offsets and offsets[0] < 1:
            messages.error(request, "The first follow-up must be at least 1 day after the introduction.")
        else:
            with transaction.atomic():
                obj = form.save()
                formset.instance = obj
                formset.save()
            audit("followup.sequence_saved", obj, user=request.user, summary=f"{obj.name}: days {offsets}")
            messages.success(request, "Follow-up schedule saved. New steps apply to active sequences from their next step.")
            return redirect("followups:enrollment_list")
    return render(request, "followups/sequence_form.html", {"form": form, "formset": formset, "seq": seq})
