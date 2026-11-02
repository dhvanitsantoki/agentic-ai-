import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from unittest import skipUnless

from django.test import TestCase

from apps.core.models import CompanyProfile, Role
from apps.core.testing import make_user
from apps.products.models import CompanyDocument, Product, ProductVariant

PRODUCTS_TS = '''export type ProductCategory = {
  slug: string;
  name: string;
};

export const categories: ProductCategory[] = [
  { slug: "ball-valves", name: "Ball Valves" },
];

export type Product = {
  slug: string;
};

export const products: Product[] = [
  {
    slug: "rpvc-double-union-ball-valve", name: "RPVC Double Union Ball Valve", category: "ball-valves",
    image: "/images/du.jpg", shortDescription: "s", description: "Main-line valve.", sizeRange: "32mm – 63mm",
    sizes: ["32mm", "63mm"], material: "RPVC (Rigid PVC), TPE seals", pressureRating: "16 bar", features: ["Quarter-turn"],
    applications: ["Drip main lines"],
  },
  {
    slug: "black-tee", name: "Black Tee", category: "fittings-accessories", image: "/images/missing.jpg", shortDescription: "s",
    description: "Tee.", sizeRange: "16mm", sizes: ["16mm"], material: "UV-stabilised Polypropylene", pressureRating: "PN 4",
    features: [], applications: [],
  },
];

export function getProductBySlug(slug: string) {
  return products.find((p) => p.slug === slug);
}
'''
COMPANY_TS = '''export const company = {
  brand: "FLOTEC", legalName: "Ajay Plastic", foundedYear: 1999,
  address: { line1: "Plot 1", line2: "Test Industrial Area", city: "Testcity", state: "Teststate", country: "India" },
  phones: ["+91 1"], whatsapp: "+911", emails: ["sales@example.test"],
} as const;

export const stats = [{ value: 40, suffix: "+", label: "Product Variants" }];

export const timeline = [
  { year: "2001", title: "Foundation", description: "Company established." },
  { year: "2030", title: "Revenue Vision", description: "Target of 100 crore." },
];
'''


@skipUnless(shutil.which("node"), "Node.js not installed")
class WebsiteImportTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "src" / "data").mkdir(parents=True)
        (self.tmp / "src" / "data" / "products.ts").write_text(PRODUCTS_TS)
        (self.tmp / "src" / "data" / "company.ts").write_text(COMPANY_TS)
        (self.tmp / "public" / "images").mkdir(parents=True)
        (self.tmp / "public" / "images" / "du.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        (self.tmp / "public" / "downloads").mkdir(parents=True)
        (self.tmp / "public" / "downloads" / "flotec-catalogue.pdf").write_bytes(b"%PDF-1.4 cat")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_is_unverified_complete_and_idempotent(self):
        out = StringIO()
        call_command("import_website_data", str(self.tmp), stdout=out)
        du = Product.objects.get(name="RPVC Double Union Ball Valve")
        self.assertEqual((du.category.name, du.material, du.is_verified), ("RPVC Double Union Ball Valves", "RPVC", False))
        self.assertEqual(du.images.count(), 1)
        self.assertEqual(sorted(du.variants.values_list("size", flat=True)), ["32mm", "63mm"])
        self.assertEqual(du.variants.get(size="32mm").size_mm, 32)
        tee = Product.objects.get(name="Black Tee")
        self.assertEqual((tee.category.name, tee.material), ("Drip Irrigation Fittings", "PP"))
        p = CompanyProfile.get_solo()
        self.assertEqual((p.year_established, p.email, p.brand_name), (1999, "sales@example.test", "Flotec"),
                         "existing brand name is kept unless --overwrite-profile")
        self.assertIn("40+ product variants", p.approved_facts)
        self.assertIn("2001: Company established.", p.approved_facts)
        self.assertNotIn("crore", p.approved_facts, "targets are not facts")
        self.assertIn("Manufacturing facility: Plot 1, Test Industrial Area, Testcity", p.approved_facts)
        self.assertEqual(p.certifications, "", "certifications are never copied automatically")
        doc = CompanyDocument.objects.get()
        self.assertFalse(doc.approved_for_sending)
        self.assertIn("NOT verified", out.getvalue())
        call_command("import_website_data", str(self.tmp), stdout=StringIO())
        self.assertEqual(Product.objects.count(), 2)
        self.assertEqual(ProductVariant.objects.count(), 3)
        self.assertEqual(du.images.count(), 1)
        self.assertEqual(CompanyDocument.objects.count(), 1)

    def test_bulk_verify_by_manager_only(self):
        call_command("import_website_data", str(self.tmp), stdout=StringIO())
        self.client.force_login(make_user("ex", Role.EXECUTIVE))
        self.assertEqual(self.client.post("/products/verify-all/", {"verified": "0"}).status_code, 403)
        self.client.force_login(make_user("mgr", Role.MANAGER))
        self.client.post("/products/verify-all/", {"verified": "0", "material": "PP"})
        self.assertEqual(list(Product.objects.filter(is_verified=True).values_list("name", flat=True)), ["Black Tee"])
