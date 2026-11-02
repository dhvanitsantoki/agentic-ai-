"""Live check of the AI provider: one small structured request with a clearly synthetic test e-mail.

    python manage.py ai_selftest                       # provider chosen in System settings for classification
    python manage.py ai_selftest --provider openrouter # force a provider for this test only (settings unchanged)
    python manage.py ai_selftest --provider anthropic --model claude-haiku-5-5

Uses one request (free with openrouter/free; a fraction of a cent on Claude Haiku). The call is
recorded as a "System" agent run, so it shows in the Agent log and counts towards the AI budget.
The same test is available as the "Test AI" button on the Automation page.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.agents.llm import ANTHROPIC, OPENROUTER
from apps.agents.selftest import describe, run_selftest


class Command(BaseCommand):
    help = "Make one live AI request (synthetic test e-mail) to check the API key, model and limits."

    def add_arguments(self, parser):
        parser.add_argument("--provider", choices=[ANTHROPIC, OPENROUTER],
                            help="Provider to test (default: the one chosen for classification in System settings).")
        parser.add_argument("--model", help="Claude model for an Anthropic test (default: the classification model).")

    def handle(self, *args, **opts):
        out = run_selftest(opts["provider"], opts["model"])
        lines = describe(out)
        for line in lines[:-1]:
            self.stdout.write(line)
        if not out["ok"]:
            raise CommandError(lines[-1])
        style = self.style.SUCCESS if out["verdict"] == "PASS" else self.style.WARNING
        self.stdout.write(style(lines[-1]))
