"""Continents, countries and country-level export market research."""
from django.conf import settings
from django.db import models


class Continent(models.Model):
    code = models.CharField(max_length=2, primary_key=True)
    name = models.CharField(max_length=40)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Country(models.Model):
    iso2 = models.CharField(max_length=2, primary_key=True)
    name = models.CharField(max_length=100)
    continent = models.ForeignKey(Continent, on_delete=models.PROTECT, related_name="countries")
    timezone = models.CharField(max_length=64, blank=True, help_text="Primary IANA time zone, used for sending windows.")
    is_priority = models.BooleanField(default=False, help_text="Include in the nightly research rotation.")
    research_notes = models.TextField(blank=True)
    last_researched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "countries"

    def __str__(self):
        return self.name


class Confidence(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class CountryMarketReport(models.Model):
    """AI- or human-prepared market overview for one country, always with sources and a confidence level."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft (unreviewed)"
        REVIEWED = "reviewed", "Reviewed by a human"
        ARCHIVED = "archived", "Archived"

    country = models.ForeignKey(Country, on_delete=models.CASCADE, related_name="market_reports")
    summary = models.TextField(blank=True)
    demand_assessment = models.TextField(blank=True, help_text="Evidence for (or against) demand for plastic valves / irrigation products.")
    demand_level = models.CharField(max_length=10, choices=[("unknown", "Unknown"), ("low", "Low"), ("medium", "Medium"), ("high", "High")], default="unknown")
    buyer_types = models.TextField(blank=True, help_text="Importers, distributors, OEM brands, irrigation companies seen in this market.")
    import_requirements = models.TextField(blank=True)
    relevant_standards = models.TextField(blank=True)
    competitors = models.TextField(blank=True)
    opportunities = models.TextField(blank=True)
    risks = models.TextField(blank=True)
    confidence = models.CharField(max_length=10, choices=Confidence.choices, default=Confidence.LOW)
    sources = models.JSONField(default=list, blank=True, help_text="List of {url, title, supports}.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    generated_by_ai = models.BooleanField(default=False)
    research_run = models.ForeignKey("research.ResearchRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="market_reports")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.country} market report ({self.created_at:%Y-%m-%d})"
