from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agents"
    label = "agents"

    def ready(self):
        from django.utils.module_loading import autodiscover_modules

        autodiscover_modules("jobs")
