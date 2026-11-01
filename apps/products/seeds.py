from .models import ProductCategory

# Categories provided by the owner in the system brief.
CATEGORIES = [
    "PP Ball Valves", "PVC Ball Valves", "CPVC Ball Valves", "UPVC Ball Valves",
    "RPVC Double Union Ball Valves", "CPVC Single Union Valves", "UPVC Union Valves",
    "PVC Compact Ball Valves", "Drip Irrigation Valves", "Drip Irrigation Fittings",
    "Other Plastic Irrigation Products",
]


def seed_categories() -> int:
    created = 0
    for i, name in enumerate(CATEGORIES):
        _, was_created = ProductCategory.objects.get_or_create(name=name, defaults={"sort_order": (i + 1) * 10})
        created += was_created
    return created
