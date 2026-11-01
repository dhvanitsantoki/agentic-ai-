"""CRM: companies (leads/customers), contacts, evidence, pipeline, activities, requirements, tasks."""
import re
from urllib.parse import urlparse

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Stage(models.TextChoices):
    NEW = "new", "New lead"
    RESEARCHED = "researched", "Research completed"
    VERIFIED = "verified", "Verified"
    CONTACTED = "contacted", "Contacted"
    REPLIED = "replied", "Replied"
    INTERESTED = "interested", "Interested"
    REQUIREMENT = "requirement", "Requirement received"
    QUOTED = "quoted", "Quotation sent"
    NEGOTIATION = "negotiation", "Negotiation"
    SAMPLE = "sample", "Sample requested"
    ORDER = "order", "Order confirmed"
    PRODUCTION = "production", "Production"
    DISPATCH = "dispatch", "Dispatch"
    PAID = "paid", "Payment received"
    LOST = "lost", "Closed / not interested"


STAGE_ORDER = [s.value for s in Stage if s != Stage.LOST]


def stage_index(stage: str) -> int:
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1


class CompanyType(models.TextChoices):
    IMPORTER = "importer", "Importer"
    DISTRIBUTOR = "distributor", "Distributor"
    WHOLESALER = "wholesaler", "Wholesaler"
    MANUFACTURER = "manufacturer", "Manufacturer"
    OEM_BRAND = "oem_brand", "Brand owner (own-brand valves)"
    IRRIGATION = "irrigation", "Irrigation company"
    PLUMBING = "plumbing", "Plumbing company"
    WATER = "water", "Water management company"
    HARDWARE = "hardware", "Hardware wholesaler"
    CONTRACTOR = "contractor", "Contractor / project company"
    RETAILER = "retailer", "Retailer"
    OTHER = "other", "Other"


class Industry(models.TextChoices):
    IRRIGATION = "irrigation", "Irrigation"
    AGRICULTURE = "agriculture", "Agriculture"
    PLUMBING = "plumbing", "Plumbing"
    WATER = "water", "Water management"
    INDUSTRIAL_VALVES = "industrial_valves", "Industrial valves"
    PIPES_FITTINGS = "pipes_fittings", "Pipes & fittings"
    HARDWARE = "hardware", "Hardware wholesale"
    OEM = "oem", "OEM manufacturing"
    IMPORT_DISTRIBUTION = "import_distribution", "Import & distribution"


class OemCategory(models.TextChoices):
    A = "A", "A – High-potential OEM buyer"
    B = "B", "B – Medium-potential OEM buyer"
    C = "C", "C – General distributor"
    D = "D", "D – Importer"
    E = "E", "E – Not relevant"


class LeadSource(models.TextChoices):
    AI_RESEARCH = "ai_research", "AI buyer research"
    AI_OEM = "ai_oem", "AI OEM research"
    MANUAL = "manual", "Manual entry"
    IMPORT = "import", "CSV import"
    EXHIBITION = "exhibition", "Exhibition"
    REFERRAL = "referral", "Referral"
    INBOUND = "inbound", "Inbound enquiry"
    EXISTING = "existing", "Existing customer"


class BusinessType(models.TextChoices):
    OEM = "oem", "OEM / private label"
    EXPORT = "export", "Export distribution"
    BOTH = "both", "OEM + export"
    UNKNOWN = "unknown", "Not yet known"


def normalize_name(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    stop = {
        "ltd", "limited", "llc", "inc", "co", "company", "corp", "corporation", "gmbh", "sa", "srl", "spa", "bv",
        "pty", "plc", "the", "sarl", "sas", "ag", "pvt", "private", "group", "trading", "est", "fze", "fzco", "lda",
    }
    return " ".join(w for w in s.split() if w not in stop).strip()


def domain_from_url(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class Company(models.Model):
    class VerificationStatus(models.TextChoices):
        UNVERIFIED = "unverified", "Unverified"
        VERIFIED = "verified", "Verified active"
        INACTIVE = "inactive", "Website inactive / unreachable"

    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, db_index=True, editable=False)
    website = models.URLField(max_length=500, blank=True)
    domain = models.CharField(max_length=255, blank=True, db_index=True, editable=False)
    country = models.ForeignKey("geo.Country", null=True, blank=True, on_delete=models.SET_NULL, related_name="companies")
    city = models.CharField(max_length=120, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=60, blank=True)
    general_email = models.EmailField(blank=True)
    linkedin_url = models.URLField(max_length=500, blank=True)

    company_type = models.CharField(max_length=20, choices=CompanyType.choices, default=CompanyType.OTHER)
    industries = models.JSONField(default=list, blank=True)
    business_type = models.CharField(max_length=10, choices=BusinessType.choices, default=BusinessType.UNKNOWN)
    description = models.TextField(blank=True)
    existing_brands = models.CharField(max_length=500, blank=True)
    product_range = models.TextField(blank=True)
    potential_requirements = models.TextField(blank=True)
    estimated_size = models.CharField(max_length=120, blank=True, help_text="Only if verifiable; cite the source.")
    estimated_size_source = models.URLField(max_length=500, blank=True)
    fit_evidence = models.TextField(blank=True)
    products_interested = models.ManyToManyField("products.ProductCategory", blank=True, related_name="interested_companies")

    oem_score = models.PositiveSmallIntegerField(default=0)
    oem_category = models.CharField(max_length=1, choices=OemCategory.choices, blank=True)
    score_breakdown = models.JSONField(default=dict, blank=True)
    research_confidence = models.CharField(max_length=10, choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], blank=True)

    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.NEW, db_index=True)
    lead_source = models.CharField(max_length=20, choices=LeadSource.choices, default=LeadSource.MANUAL)
    verification_status = models.CharField(max_length=12, choices=VerificationStatus.choices, default=VerificationStatus.UNVERIFIED)
    website_checked_at = models.DateTimeField(null=True, blank=True)
    do_not_contact = models.BooleanField(default=False)
    is_existing_customer = models.BooleanField(default=False)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="companies")
    expected_order_quantity = models.CharField(max_length=200, blank=True)
    expected_order_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    expected_value_currency = models.CharField(max_length=3, default="USD")
    negotiation_status = models.CharField(max_length=255, blank=True)
    next_action = models.CharField(max_length=255, blank=True)
    next_action_date = models.DateField(null=True, blank=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    last_reply_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    research_run = models.ForeignKey("research.ResearchRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="companies")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name_plural = "companies"
        indexes = [models.Index(fields=["country", "stage"]), models.Index(fields=["oem_category"])]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.normalized_name = normalize_name(self.name)
        self.domain = domain_from_url(self.website)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("crm:company_detail", args=[self.pk])

    @property
    def industry_labels(self):
        labels = dict(Industry.choices)
        return [labels.get(i, i) for i in self.industries or []]

    @property
    def primary_contact(self):
        return self.contacts.filter(is_primary=True).first() or self.contacts.first()

    @property
    def local_timezone(self):
        return self.country.timezone if self.country_id and self.country.timezone else "UTC"


class CompanySource(models.Model):
    """A public source that supports a fact about a company (evidence trail)."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="sources")
    url = models.URLField(max_length=1000)
    title = models.CharField(max_length=300, blank=True)
    supports = models.TextField(blank=True, help_text="What this source shows (quote or summary).")
    kind = models.CharField(max_length=20, default="website", choices=[
        ("website", "Company website"), ("catalogue", "Catalogue"), ("directory", "Business directory"),
        ("exhibition", "Exhibition list"), ("news", "News / article"), ("linkedin", "LinkedIn"), ("other", "Other"),
    ])
    verified_reachable = models.BooleanField(null=True, help_text="Source page fetched successfully during verification.")
    retrieved_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-retrieved_at"]

    def __str__(self):
        return self.url


class Contact(models.Model):
    class EmailStatus(models.TextChoices):
        UNVERIFIED = "unverified", "Unverified – do not e-mail"
        VERIFIED = "verified", "Verified (published on source page, MX ok)"
        INVALID = "invalid", "Invalid"
        BOUNCED = "bounced", "Bounced"
        MANUAL = "manual", "Confirmed by a team member"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="contacts")
    full_name = models.CharField(max_length=200, blank=True, help_text="Leave blank for generic addresses like info@.")
    title = models.CharField(max_length=200, blank=True)
    department = models.CharField(max_length=120, blank=True)
    is_decision_maker = models.BooleanField(default=False)
    is_primary = models.BooleanField(default=False)
    email = models.EmailField(blank=True, db_index=True)
    email_status = models.CharField(max_length=12, choices=EmailStatus.choices, default=EmailStatus.UNVERIFIED)
    email_checks = models.JSONField(default=dict, blank=True)
    email_source_url = models.URLField(max_length=1000, blank=True)
    phone = models.CharField(max_length=60, blank=True)
    whatsapp = models.CharField(max_length=60, blank=True)
    phone_source_url = models.URLField(max_length=1000, blank=True)
    linkedin_url = models.URLField(max_length=500, blank=True)
    do_not_contact = models.BooleanField(default=False)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_primary", "-is_decision_maker", "full_name"]

    def __str__(self):
        label = self.full_name or self.email or f"Contact {self.pk}"
        return f"{label} ({self.company})"

    @property
    def display_name(self):
        return self.full_name or self.email

    @property
    def is_emailable(self) -> bool:
        return bool(self.email) and self.email_status in (self.EmailStatus.VERIFIED, self.EmailStatus.MANUAL) and not self.do_not_contact

    @property
    def is_role_address(self) -> bool:
        local = (self.email or "").split("@")[0].lower()
        return local in {"info", "sales", "export", "contact", "office", "enquiry", "enquiries", "purchase", "procurement", "admin", "import"}


class StageHistory(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="stage_history")
    from_stage = models.CharField(max_length=20, choices=Stage.choices, blank=True)
    to_stage = models.CharField(max_length=20, choices=Stage.choices)
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    agent = models.CharField(max_length=40, blank=True)
    reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-changed_at"]


class Activity(models.Model):
    class Kind(models.TextChoices):
        EMAIL_OUT = "email_out", "E-mail sent"
        EMAIL_IN = "email_in", "E-mail received"
        CALL = "call", "Phone call"
        WHATSAPP = "whatsapp", "WhatsApp"
        MEETING = "meeting", "Meeting"
        NOTE = "note", "Note"
        STAGE = "stage", "Stage change"
        QUOTATION = "quotation", "Quotation"
        SAMPLE = "sample", "Sample"
        SYSTEM = "system", "System / agent"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="activities")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="activities")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    subject = models.CharField(max_length=300, blank=True)
    body = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    agent = models.CharField(max_length=40, blank=True)
    outbound_email = models.ForeignKey("outreach.OutboundEmail", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    inbound_email = models.ForeignKey("mailbox.InboundEmail", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-occurred_at"]
        verbose_name_plural = "activities"

    def __str__(self):
        return f"{self.get_kind_display()}: {self.subject}"


class Requirement(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        QUOTED = "quoted", "Quoted"
        CLOSED = "closed", "Closed"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="requirements")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL)
    product_category = models.ForeignKey("products.ProductCategory", null=True, blank=True, on_delete=models.SET_NULL)
    description = models.TextField()
    material = models.CharField(max_length=40, blank=True)
    sizes = models.CharField(max_length=255, blank=True)
    pressure_rating = models.CharField(max_length=80, blank=True)
    connection_type = models.CharField(max_length=120, blank=True)
    quantity = models.CharField(max_length=120, blank=True)
    target_price = models.CharField(max_length=120, blank=True, help_text="As stated by the customer.")
    branding = models.CharField(max_length=255, blank=True, help_text="Private label / OEM branding needs.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    source_email = models.ForeignKey("mailbox.InboundEmail", null=True, blank=True, on_delete=models.SET_NULL, related_name="requirements")
    created_by_agent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.company}: {self.description[:60]}"


class Task(models.Model):
    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    class Kind(models.TextChoices):
        ESCALATION = "escalation", "Escalation from AI"
        FOLLOWUP = "followup", "Follow-up"
        CALL = "call", "Call"
        APPROVAL = "approval", "Approval / decision"
        REVIEW = "review", "Review lead"
        OTHER = "other", "Other"

    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=255)
    details = models.TextField(blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OTHER)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    due_date = models.DateField(null=True, blank=True)
    done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by_agent = models.CharField(max_length=40, blank=True)
    link = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["done", "due_date", "-created_at"]

    def __str__(self):
        return self.title
