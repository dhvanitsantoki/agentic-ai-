from django.core.management.base import BaseCommand

from apps.agents.worker import run_forever, work_once


class Command(BaseCommand):
    help = "Run the background job worker and scheduler (keep running under systemd)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process due jobs once and exit.")
        parser.add_argument("--sleep", type=int, default=15)

    def handle(self, *args, **opts):
        if opts["once"]:
            n = work_once()
            self.stdout.write(f"Processed {n} job(s).")
        else:
            run_forever(opts["sleep"])
