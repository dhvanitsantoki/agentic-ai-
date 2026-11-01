from .models import ScheduledTask
from .scheduler import compute_next_run

DEFAULT_SCHEDULE = [
    ("Nightly research & outreach pipeline", "pipeline.nightly", "0 2 * * *", {},
     "Discover companies, verify, dedupe, score, add to CRM, prepare drafts, send per policy, schedule follow-ups, report."),
    ("Mailbox sync", "mailbox.sync_all", "*/10 * * * *", {}, "Fetch and process new e-mails from all connected mailboxes."),
    ("Send approved e-mails", "outreach.send_queue", "*/5 * * * *", {}, "Send approved/auto-approved e-mails inside recipient business hours."),
    ("Process follow-ups", "followups.process_due", "20 * * * *", {}, "Prepare due follow-ups and apply stop rules."),
    ("CRM hygiene", "crm.hygiene", "0 6 * * *", {}, "Flag overdue next actions and stale leads; create tasks."),
    ("Expire quotations", "quotations.expire", "10 0 * * *", {}, "Mark quotations past their validity date as expired."),
    ("Daily management report", "reports.generate", "30 7 * * *", {"period": "daily"}, "Yesterday's report, e-mailed to report recipients."),
    ("Weekly management report", "reports.generate", "45 7 * * 1", {"period": "weekly"}, "Last week's report (Mondays)."),
    ("Monthly management report", "reports.generate", "0 8 1 * *", {"period": "monthly"}, "Last month's report (1st of month)."),
    ("Database backup", "system.backup", "30 1 * * *", {}, "Database dump + media archive with rotation."),
]


def seed_schedule() -> int:
    created = 0
    for name, kind, cron, payload, description in DEFAULT_SCHEDULE:
        task, was_created = ScheduledTask.objects.get_or_create(
            name=name, defaults={"job_kind": kind, "cron": cron, "payload": payload, "description": description}
        )
        if was_created:
            task.next_run_at = compute_next_run(cron)
            task.save(update_fields=["next_run_at"])
        created += was_created
    return created
