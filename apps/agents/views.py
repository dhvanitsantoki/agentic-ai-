from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.models import AutomationState, ExecutionMode, SystemSettings
from apps.core.permissions import admin_required, manager_required, safe_next

from .forms import ScheduledTaskForm
from .llm import ai_configured, month_spend
from .models import AgentName, AgentRun, Job, ScheduledTask
from .registry import HANDLERS, enqueue
from .scheduler import compute_next_run
from .worker import cancel_queued_jobs


def control(request):
    sys = SystemSettings.get_solo()
    since = timezone.now() - timezone.timedelta(days=7)
    per_agent = (AgentRun.objects.filter(started_at__gte=since).values("agent")
                 .annotate(n=Count("id"), cost=Sum("cost_usd"), failed=Count("id", filter=Q(status="failed")))
                 .order_by("agent"))
    labels = dict(AgentName.choices)
    return render(request, "agents/control.html", {
        "sys": sys,
        "tasks": ScheduledTask.objects.all(),
        "jobs": Job.objects.select_related("created_by")[:15],
        "queued": Job.objects.filter(status=Job.Status.QUEUED).count(),
        "running": Job.objects.filter(status=Job.Status.RUNNING).count(),
        "failed_recent": AgentRun.objects.filter(status=AgentRun.Status.FAILED).select_related("job")[:10],
        "per_agent": [{**row, "label": labels.get(row["agent"], row["agent"])} for row in per_agent],
        "spend": month_spend(),
        "ai_configured": ai_configured(),
        "modes": ExecutionMode.choices,
        "last_worker_activity": Job.objects.exclude(started_at=None).order_by("-started_at").values_list("started_at", flat=True).first(),
    })


@manager_required
@require_POST
def set_state(request):
    sys = SystemSettings.get_solo()
    state = request.POST.get("state")
    if state not in AutomationState.values:
        messages.error(request, "Unknown state.")
        return redirect("agents:control")
    sys.set_automation_state(state, request.user)
    cancelled = 0
    if state == AutomationState.STOPPED:
        cancelled = cancel_queued_jobs(f"Stopped by {request.user.username}")
    audit("automation.state", sys, user=request.user, summary=f"Automation {state}", cancelled_jobs=cancelled)
    msg = {"running": "Automation resumed.", "paused": "Automation paused - scheduled jobs will not start; manual actions still run.",
           "stopped": f"Emergency stop: all automation halted, {cancelled} queued job(s) cancelled. Nothing will be sent."}[state]
    messages.warning(request, msg) if state != "running" else messages.success(request, msg)
    return redirect(safe_next(request, "agents:control"))


@admin_required
@require_POST
def set_mode(request):
    sys = SystemSettings.get_solo()
    try:
        mode = int(request.POST.get("mode"))
    except (TypeError, ValueError):
        mode = None
    if mode not in ExecutionMode.values:
        messages.error(request, "Unknown mode.")
        return redirect("agents:control")
    before = sys.execution_mode
    sys.execution_mode = mode
    sys.save(update_fields=["execution_mode", "updated_at"])
    audit("automation.mode", sys, user=request.user, summary=f"Execution mode {before} → {mode}")
    messages.success(request, f"Execution mode set to {sys.get_execution_mode_display()}.")
    return redirect("agents:control")


@manager_required
def task_edit(request, pk):
    task = get_object_or_404(ScheduledTask, pk=pk)
    form = ScheduledTaskForm(request.POST or None, instance=task)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.next_run_at = compute_next_run(obj.cron)
        obj.save()
        audit("automation.schedule", obj, user=request.user, summary=f"{obj.name}: {obj.cron} enabled={obj.enabled}")
        messages.success(request, "Schedule saved.")
        return redirect("agents:control")
    return render(request, "core/form_page.html", {"form": form, "title": f"Schedule: {task.name}",
                                                   "subtitle": f"Job: {task.job_kind}. Times use the server time zone."})


@manager_required
@require_POST
def task_run_now(request, pk):
    task = get_object_or_404(ScheduledTask, pk=pk)
    if SystemSettings.get_solo().automation_state == AutomationState.STOPPED:
        messages.error(request, "Automation is stopped. Resume it first.")
        return redirect("agents:control")
    job = enqueue(task.job_kind, task.payload, user=request.user, scheduled_task=task)
    audit("automation.run_now", task, user=request.user, summary=f"Manual run of {task.name}")
    messages.success(request, f"'{task.name}' queued as job #{job.pk}.")
    return redirect("agents:control")


def job_list(request):
    qs = Job.objects.select_related("created_by", "scheduled_task")
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    if request.GET.get("kind"):
        qs = qs.filter(kind=request.GET["kind"])
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "agents/job_list.html", {"page_obj": page, "statuses": Job.Status.choices,
                                                    "kinds": sorted(HANDLERS), "f": request.GET})


def job_detail(request, pk):
    job = get_object_or_404(Job, pk=pk)
    return render(request, "agents/job_detail.html", {"job": job, "runs": job.agent_runs.all()})


@manager_required
@require_POST
def job_retry(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if job.status in (Job.Status.FAILED, Job.Status.CANCELLED):
        job.status = Job.Status.QUEUED
        job.attempts = 0
        job.run_after = timezone.now()
        job.created_by = job.created_by or request.user
        job.save(update_fields=["status", "attempts", "run_after", "created_by"])
        messages.success(request, "Job re-queued.")
    return redirect("agents:job_detail", pk=pk)


def run_log(request):
    qs = AgentRun.objects.select_related("job")
    if request.GET.get("agent"):
        qs = qs.filter(agent=request.GET["agent"])
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    totals = qs.aggregate(cost=Sum("cost_usd"), tin=Sum("input_tokens"), tout=Sum("output_tokens"), searches=Sum("web_searches"))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "agents/run_log.html", {"page_obj": page, "agents": AgentName.choices,
                                                   "statuses": AgentRun.Status.choices, "f": request.GET, "totals": totals})
