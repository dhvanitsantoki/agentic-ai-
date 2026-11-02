"""Import company details, products, sizes, photos and the catalogue from the FLOTEC website repository.

Usage:  python manage.py import_website_data /path/to/flotec-website-  [--overwrite-profile]

Reads src/data/{company,products}.ts from a checkout of the website (Node.js is used to read the
TypeScript data files). Website data stays in that private repository; it is copied only into this
system's database and media folder.

Safety rules applied:
* products and sizes are imported as NOT verified - a manager verifies them before the AI uses them;
* certifications, export-market claims and material-safety claims are NOT copied into the approved
  facts - they are printed so the owner can confirm them first;
* no prices, MOQs or packaging are invented (the website has none);
* the catalogue is uploaded but not approved for sending until a manager ticks it.
"""
import json
import re
import subprocess
import tempfile
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from apps.core.models import CompanyProfile
from apps.products.models import CompanyDocument, DocumentKind, Material, Product, ProductCategory, ProductImage, ProductVariant
from apps.products.seeds import seed_categories

# Website product -> system category (the categories from the owner's brief).
CATEGORY_RULES = [
    (lambda p: "double union ball" in p["name"].lower() and "rpvc" in p["name"].lower(), "RPVC Double Union Ball Valves"),
    (lambda p: "cpvc" in p["name"].lower(), "CPVC Ball Valves"),
    (lambda p: "upvc union" in p["name"].lower(), "UPVC Union Valves"),
    (lambda p: "upvc" in p["name"].lower() and "ball" in p["name"].lower(), "UPVC Ball Valves"),
    (lambda p: p["category"] in ("ball-valves", "specialty-valves") and "ball valve" in p["name"].lower(), "PVC Ball Valves"),
    (lambda p: p["category"] == "non-return-flanged" or "flush valve" in p["name"].lower() or p["name"].lower() == "air valve",
     "Drip Irrigation Valves"),
    (lambda p: p["category"] == "filters" or "venturi" in p["name"].lower(), "Other Plastic Irrigation Products"),
    (lambda p: p["category"] in ("fittings-accessories", "flush-valves-unions"), "Drip Irrigation Fittings"),
]


def material_code(text: str) -> str:
    t = (text or "").upper()
    for prefix, code in (("RPVC", "RPVC"), ("UPVC", "UPVC"), ("CPVC", "CPVC"), ("ABS", "ABS")):
        if t.startswith(prefix):
            return code
    if "COMPOSITE" in t or "RUBBER" in t or "POLYESTER" in t or "POLYMER HOUSING" in t:
        return Material.OTHER
    if "POLYPROPYLENE" in t or t.startswith("PP") or "(PP)" in t:
        return Material.PP
    if "PVC" in t:
        return Material.PVC
    return Material.OTHER


def size_mm(size: str):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*mm\b", size or "")
    return m.group(1) if m and "×" not in size and "x" not in size.lower().split("mm")[0] else None


def strip_typescript(ts: str) -> str:
    ts = ts.split("\nexport function")[0]
    ts = re.sub(r"export type \w+ = \{[\s\S]*?\n\};\n", "", ts)
    ts = re.sub(r"export const (\w+)\s*:\s*[\w\[\]<>, |]+\s*=", r"export const \1 =", ts)
    return ts.replace(" as const", "")


def read_website(root: Path) -> dict:
    data_dir = root / "src" / "data"
    if not (data_dir / "products.ts").exists():
        raise CommandError(f"{data_dir}/products.ts not found - pass the website repository folder.")
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("products", "company"):
            (Path(tmp) / f"{name}.mjs").write_text(strip_typescript((data_dir / f"{name}.ts").read_text(encoding="utf-8")))
        js = ("const p=await import('./products.mjs');const c=await import('./company.mjs');"
              "console.log(JSON.stringify({categories:p.categories,products:p.products,company:c.company,stats:c.stats,timeline:c.timeline||[]}))")
        try:
            res = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, cwd=tmp, timeout=60)
        except FileNotFoundError as exc:
            raise CommandError("Node.js is required to read the website data files (apt install nodejs).") from exc
    if res.returncode != 0:
        raise CommandError(f"Could not read the website data: {res.stderr[:500]}")
    return json.loads(res.stdout)


class Command(BaseCommand):
    help = "Import company profile, products, sizes, photos and catalogue from the FLOTEC website repository."

    def add_arguments(self, parser):
        parser.add_argument("website_dir")
        parser.add_argument("--overwrite-profile", action="store_true", help="Replace company profile fields that are already filled.")

    def handle(self, website_dir, overwrite_profile=False, **opts):
        root = Path(website_dir).resolve()
        data = read_website(root)
        seed_categories()
        with transaction.atomic():
            self._profile(data, overwrite_profile)
            counts = self._products(data, root)
            doc = self._catalogue(root)
        self.stdout.write(self.style.SUCCESS(
            f"Imported {counts['products_new']} new / {counts['products_updated']} updated products, "
            f"{counts['sizes']} sizes, {counts['images']} photos{', catalogue uploaded' if doc else ''}."))
        self.stdout.write("All products are NOT verified yet: a manager must review and verify them (Products page).")
        self.stdout.write("Not imported - confirm before adding to Settings > Company profile: website certification list "
                          "(e.g. ISO 9001:2015), lead-free / potable-water / food-grade claims, export country list, revenue target.")

    def _profile(self, data, overwrite):
        c = data["company"]
        p = CompanyProfile.get_solo()
        stats = {s["label"]: s for s in data.get("stats", [])}
        variants = stats.get("Product Variants", {}).get("value")
        size_stat = stats.get("Size Range Engineered", {})
        size_range = f"{size_stat.get('value')}{size_stat.get('suffix', '')}" if size_stat else ""
        # Company history from the website timeline (targets / revenue goals are not facts and are skipped).
        history = [f"{t['year']}: {t['description']}" for t in data.get("timeline", [])
                   if t.get("description") and not re.search(r"vision|target|₹|crore", f"{t.get('title', '')} {t['description']}", re.I)]
        values = {
            "legal_name": c.get("legalName", ""), "brand_name": c.get("brand", ""),
            "address": "\n".join(x for x in [c["address"].get("line1", ""), c["address"].get("line2", "")] if x),
            "city": c["address"].get("city", ""), "state": c["address"].get("state", ""), "country": c["address"].get("country", ""),
            "phone": ", ".join(c.get("phones", [])[:2]), "whatsapp": c.get("whatsapp", ""),
            "email": (c.get("emails") or [""])[0], "year_established": c.get("foundedYear"),
            "about": (f"{c.get('legalName', 'Ajay Plastic')} (brand {c.get('brand', 'FLOTEC')}), established in {c.get('foundedYear')} in "
                      f"{c['address'].get('city', '')}, {c['address'].get('state', '')}, {c['address'].get('country', '')}, manufactures plastic "
                      "valves - RPVC, UPVC, CPVC and PVC ball valves, non-return, flanged and flush valves - together with screen filters and "
                      "drip-irrigation fittings for agriculture, landscape irrigation and water management."),
            "approved_facts": "\n".join(x for x in [
                f"Established in {c.get('foundedYear')} in {c['address'].get('city', '')}, {c['address'].get('state', '')}, {c['address'].get('country', '')}",
                "Manufacturing facility: " + ", ".join(v for v in [c["address"].get("line1", ""), c["address"].get("line2", ""),
                                                                    c["address"].get("city", "")] if v),
                f"{variants}+ product variants" + (f" in sizes {size_range}" if size_range else "") if variants else "",
                *history,
            ] if x),
            "manufacturing_capabilities": "\n".join([
                "High-precision injection moulding with in-house tooling",
                "Valve assembly under documented work instructions",
                "Export-grade carton packing and palletising",
                "On-site finished-goods warehouse for faster dispatch",
            ]),
            "oem_services": "OEM supply for distributors and brand owners (specifications agreed per customer)",
            "sender_name": "Export Sales Team",
        }
        changed = []
        for field, value in values.items():
            if value in (None, ""):
                continue
            if overwrite or not getattr(p, field):
                setattr(p, field, value)
                changed.append(field)
        p.save()
        self.stdout.write(f"Company profile fields filled: {', '.join(changed) or 'none (already filled)'}")

    def _category_for(self, product):
        for rule, name in CATEGORY_RULES:
            if rule(product):
                return ProductCategory.objects.get(name=name)
        return ProductCategory.objects.get(name="Other Plastic Irrigation Products")

    def _products(self, data, root):
        counts = {"products_new": 0, "products_updated": 0, "sizes": 0, "images": 0}
        for wp in data["products"]:
            category = self._category_for(wp)
            material = material_code(wp.get("material", ""))
            features = "\n".join(wp.get("features", []))
            fields = {
                "description": wp.get("description") or wp.get("shortDescription", ""),
                "applications": "; ".join(wp.get("applications", [])),
                "technical_specifications": f"Material: {wp.get('material', '')}\n{features}".strip(),
                "available_sizes": (wp.get("sizeRange") or ", ".join(wp.get("sizes", [])))[:255],
                "pressure_rating": (wp.get("pressureRating") or "")[:120],
            }
            product, created = Product.objects.get_or_create(category=category, name=wp["name"][:200], material=material, defaults=fields)
            if not created:
                for k, v in fields.items():
                    setattr(product, k, v)
                product.save()
            counts["products_new" if created else "products_updated"] += 1
            for size in wp.get("sizes", []):
                sku = slugify(f"flt-{wp['slug']}-{size}").upper()[:80]
                ProductVariant.objects.update_or_create(sku=sku, defaults={
                    "product": product, "size": size[:40], "size_mm": size_mm(size), "is_active": True})
                counts["sizes"] += 1
            if not product.images.exists():
                for i, rel in enumerate([wp.get("image")] + list(wp.get("gallery") or [])):
                    path = root / "public" / (rel or "").lstrip("/")
                    if rel and path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                        with path.open("rb") as fh:
                            img = ProductImage(product=product, caption=wp["name"][:200], sort_order=i)
                            img.image.save(path.name, File(fh), save=True)
                        counts["images"] += 1
        return counts

    def _catalogue(self, root):
        path = root / "public" / "downloads" / "flotec-catalogue.pdf"
        title = "FLOTEC product catalogue"
        if not path.is_file() or CompanyDocument.objects.filter(title=title).exists():
            return None
        doc = CompanyDocument(title=title, kind=DocumentKind.CATALOGUE, approved_for_sending=False,
                              notes="Imported from the company website. Tick 'approved for sending' after checking it is current.")
        with path.open("rb") as fh:
            doc.file.save(path.name, File(fh), save=True)
        doc.categories.set(ProductCategory.objects.all())
        return doc
