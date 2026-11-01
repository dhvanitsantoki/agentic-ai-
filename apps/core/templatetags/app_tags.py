from django import template
from django.utils.http import urlencode

register = template.Library()

STAGE_COLOURS = {
    "new": "secondary", "researched": "info", "verified": "info", "contacted": "primary", "replied": "primary",
    "interested": "success", "requirement": "success", "quoted": "warning", "negotiation": "warning",
    "sample": "warning", "order": "success", "production": "success", "dispatch": "success", "paid": "dark", "lost": "light",
}
CATEGORY_COLOURS = {"A": "success", "B": "primary", "C": "info", "D": "secondary", "E": "light"}
EMAIL_STATUS_COLOURS = {"verified": "success", "manual": "success", "unverified": "warning", "invalid": "danger", "bounced": "danger"}
OUT_STATUS_COLOURS = {"draft": "secondary", "pending_approval": "warning", "approved": "info", "sending": "info", "sent": "success",
                      "failed": "danger", "rejected": "dark", "cancelled": "light", "blocked": "danger"}


@register.filter
def stage_colour(value):
    return STAGE_COLOURS.get(value, "secondary")


@register.filter
def category_colour(value):
    return CATEGORY_COLOURS.get(value, "light")


@register.filter
def email_status_colour(value):
    return EMAIL_STATUS_COLOURS.get(value, "secondary")


@register.filter
def out_status_colour(value):
    return OUT_STATUS_COLOURS.get(value, "secondary")


@register.simple_tag(takes_context=True)
def query_with(context, **kwargs):
    params = context["request"].GET.copy()
    for k, v in kwargs.items():
        params[k] = v
    return urlencode(params)


@register.filter
def get_item(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def money(value, currency=""):
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.2f} {currency}".strip()
    except (TypeError, ValueError):
        return value
