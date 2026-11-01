from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import CompanyProfile, Notification, Role, SystemSettings
from .permissions import is_manager, role_of


def app_context(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from apps.agents.models import AgentRun
    from apps.crm.models import Task
    from apps.mailbox.models import InboundEmail
    from apps.outreach.models import OutboundEmail
    from apps.quotations.models import Quotation
    from apps.research.models import ResearchFinding

    user = request.user
    profile = CompanyProfile.get_solo()
    sys = SystemSettings.get_solo()
    notif_q = Q(user=user)
    if is_manager(user):
        notif_q |= Q(user__isnull=True)
    since = timezone.now() - timezone.timedelta(days=1)
    counts = {
        "approvals": OutboundEmail.objects.filter(status=OutboundEmail.Status.PENDING_APPROVAL).count(),
        "quotations": Quotation.objects.filter(status=Quotation.Status.PENDING_APPROVAL).count(),
        "inbox": InboundEmail.objects.filter(needs_attention=True).count(),
        "review": ResearchFinding.objects.filter(status=ResearchFinding.Status.NEEDS_REVIEW).count(),
        "tasks_due": Task.objects.filter(done=False, due_date__lte=timezone.localdate(), assigned_to=user).count(),
        "notifications": Notification.objects.filter(notif_q, is_read=False).count(),
        "agent_errors": AgentRun.objects.filter(status=AgentRun.Status.FAILED, started_at__gte=since).count(),
    }
    role = role_of(user)
    return {
        "app_brand": profile.brand_name,
        "app_company": profile.legal_name,
        "app_settings": sys,
        "today": timezone.localdate(),
        "now": timezone.now(),
        "ai_configured": bool(settings.ANTHROPIC_API_KEY),
        "nav_counts": counts,
        "user_role": role,
        "user_role_label": Role(role).label if role else "",
        "user_is_manager": is_manager(user),
        "user_is_admin": role == Role.ADMIN,
        "user_can_edit": role in (Role.ADMIN, Role.MANAGER, Role.EXECUTIVE),
    }
