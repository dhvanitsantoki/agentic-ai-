from django.core.management.base import BaseCommand

from apps.agents.seeds import seed_schedule
from apps.core.models import CompanyProfile, SystemSettings
from apps.followups.seeds import seed_sequences
from apps.geo.seeds import seed_countries
from apps.outreach.seeds import seed_templates
from apps.products.seeds import seed_categories


class Command(BaseCommand):
    help = "Create reference data (countries, product categories, e-mail templates, follow-up schedule, scheduled tasks). Idempotent."

    def handle(self, *args, **options):
        CompanyProfile.get_solo()
        SystemSettings.get_solo()
        self.stdout.write(f"Countries created: {seed_countries()}")
        self.stdout.write(f"Product categories created: {seed_categories()}")
        self.stdout.write(f"E-mail templates created: {seed_templates()}")
        self.stdout.write(f"Follow-up sequences created: {seed_sequences()}")
        self.stdout.write(f"Scheduled tasks created: {seed_schedule()}")
        self.stdout.write(self.style.SUCCESS("Initial data ready. Automation starts PAUSED - resume it in Automation once setup is complete."))
