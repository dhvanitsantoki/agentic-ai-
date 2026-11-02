"""Serverless (Vercel) replacement for the long-running worker.

Vercel Cron calls GET /cron/tick/ every minute with the header "Authorization: Bearer <CRON_SECRET>".
Each call schedules due tasks and processes queued jobs for a limited time (see worker.process_tick).
"""
import hmac

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .worker import process_tick


def cron_authorized(request) -> bool:
    secret = settings.CRON_SECRET
    supplied = request.headers.get("Authorization", "")
    return bool(secret) and hmac.compare_digest(supplied.encode(), f"Bearer {secret}".encode())


@login_not_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
def tick(request):
    if not settings.CRON_SECRET:
        return JsonResponse({"error": "CRON_SECRET is not configured"}, status=503)
    if not cron_authorized(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(process_tick(settings.CRON_TICK_BUDGET_SECONDS, settings.CRON_MAX_RUNNING_JOBS))
