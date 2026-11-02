from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.permissions import editor_required, manager_required

from .forms import (
    CategoryForm, CsvUploadForm, DocumentForm, ImageForm, PriceItemForm, PriceListForm, ProductForm,
    VariantAddForm, VariantForm, parse_decimal,
)
from .importers import PRICE_COLUMNS, PRODUCT_COLUMNS, SIZE_COLUMNS, VARIANT_COLUMNS, import_products, import_sizes
from .models import CompanyDocument, Material, PriceList, PriceListItem, Product, ProductCategory, ProductImage, ProductVariant


def product_list(request):
    qs = Product.objects.select_related("category").annotate(n_variants=Count("variants"))
    category = request.GET.get("category")
    material = request.GET.get("material")
    verified = request.GET.get("verified")
    q = request.GET.get("q", "").strip()
    if category:
        qs = qs.filter(category_id=category)
    if material:
        qs = qs.filter(material=material)
    if verified in ("1", "0"):
        qs = qs.filter(is_verified=verified == "1")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(model_code__icontains=q) | Q(variants__sku__icontains=q)).distinct()
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    categories = ProductCategory.objects.annotate(n=Count("products"))
    return render(request, "products/product_list.html", {
        "page_obj": page, "categories": categories, "materials": Material.choices,
        "filters": {"category": category or "", "material": material or "", "verified": verified or "", "q": q},
    })


def product_detail(request, pk):
    product = get_object_or_404(Product.objects.select_related("category", "verified_by"), pk=pk)
    variants = product.variants.all()
    price_items = (
        PriceListItem.objects.filter(variant__product=product)
        .select_related("price_list", "variant")
        .order_by("variant__size_mm", "variant__size", "price_list__currency", "min_quantity")
    )
    return render(request, "products/product_detail.html", {
        "product": product, "variants": variants, "images": product.images.all(),
        "documents": product.documents.all(), "price_items": price_items,
        "missing": product.missing_fields(),
        "variant_form": VariantAddForm(), "image_form": ImageForm(),
    })


@editor_required
def product_edit(request, pk=None):
    product = get_object_or_404(Product, pk=pk) if pk else None
    form = ProductForm(request.POST or None, request.FILES or None, instance=product)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        changed_tech = product is not None and any(
            f in form.changed_data for f in ("technical_specifications", "pressure_rating", "standards", "connection_types", "material")
        )
        if changed_tech and obj.is_verified:
            obj.is_verified = False  # technical changes must be re-verified
            messages.warning(request, "Technical data changed - the product must be verified again.")
        obj.save()
        audit("product.save", obj, user=request.user, summary=f"Product {obj} saved", changed=form.changed_data)
        messages.success(request, "Product saved.")
        return redirect("products:product_detail", pk=obj.pk)
    return render(request, "core/form_page.html", {
        "form": form, "multipart": True,
        "title": f"Edit {product}" if product else "New product",
        "subtitle": "Enter verified company information only.",
    })


@manager_required
@require_POST
def product_verify(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.POST.get("action") == "unverify":
        product.is_verified = False
        product.save(update_fields=["is_verified", "updated_at"])
        audit("product.unverify", product, user=request.user, summary=f"{product} marked unverified")
        messages.info(request, "Product marked as not verified.")
    else:
        product.mark_verified(request.user)
        audit("product.verify", product, user=request.user, summary=f"{product} verified")
        messages.success(request, "Product verified. The AI agents may now reference it.")
    return redirect("products:product_detail", pk=pk)


@editor_required
@require_POST
def variant_add(request, pk):
    product = get_object_or_404(Product, pk=pk)
    form = VariantAddForm(request.POST)
    if form.is_valid():
        v = form.save(commit=False)
        v.product = product
        v.save()
        audit("product.variant_add", v, user=request.user, summary=f"Variant {v.sku} added")
        messages.success(request, f"Size {v.size} added.")
    else:
        messages.error(request, "Could not add size: " + "; ".join(f"{k}: {', '.join(e)}" for k, e in form.errors.items()))
    return redirect("products:product_detail", pk=pk)


@editor_required
def variant_edit(request, pk):
    variant = get_object_or_404(ProductVariant, pk=pk)
    form = VariantForm(request.POST or None, instance=variant)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit("product.variant_edit", variant, user=request.user, summary=f"Variant {variant.sku} edited")
        messages.success(request, "Size updated.")
        return redirect("products:product_detail", pk=variant.product_id)
    return render(request, "core/form_page.html", {"form": form, "title": f"Edit size {variant}"})


@editor_required
@require_POST
def variant_delete(request, pk):
    variant = get_object_or_404(ProductVariant, pk=pk)
    product_id = variant.product_id
    if variant.price_items.exists():
        variant.is_active = False
        variant.save(update_fields=["is_active"])
        messages.info(request, "This size has prices recorded, so it was deactivated instead of deleted.")
    else:
        variant.delete()
        messages.success(request, "Size deleted.")
    audit("product.variant_delete", user=request.user, summary=f"Variant {pk} removed/deactivated")
    return redirect("products:product_detail", pk=product_id)


@editor_required
def variant_import(request, pk):
    """CSV columns: sku,size,size_mm,pressure_rating,connection_type,moq,packaging,unit_weight_g"""
    product = get_object_or_404(Product, pk=pk)
    form = CsvUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            rows = form.read_rows()
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("products:variant_import", pk=pk)
        created, updated, errors = import_sizes(rows, product)
        audit("product.variant_import", product, user=request.user, summary=f"{created} created, {updated} updated",
              errors=errors[:50])
        messages.success(request, f"Import finished: {created} created, {updated} updated, {len(errors)} errors.")
        for e in errors[:10]:
            messages.warning(request, e)
        return redirect("products:product_detail", pk=pk)
    return render(request, "core/form_page.html", {
        "form": form, "multipart": True, "title": f"Import sizes for {product}",
        "subtitle": "CSV columns: " + ", ".join(VARIANT_COLUMNS), "submit_label": "Import",
        "download_url": reverse("products:csv_template", args=["sizes-for-one-product"]), "download_label": "Download CSV template",
    })


@editor_required
@require_POST
def image_add(request, pk):
    product = get_object_or_404(Product, pk=pk)
    form = ImageForm(request.POST, request.FILES)
    if form.is_valid():
        img = form.save(commit=False)
        img.product = product
        img.save()
        messages.success(request, "Image uploaded.")
    else:
        messages.error(request, "Image upload failed: " + "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect("products:product_detail", pk=pk)


@editor_required
@require_POST
def image_delete(request, pk):
    img = get_object_or_404(ProductImage, pk=pk)
    product_id = img.product_id
    img.image.delete(save=False)
    img.delete()
    messages.success(request, "Image deleted.")
    return redirect("products:product_detail", pk=product_id)


def category_list(request):
    cats = ProductCategory.objects.annotate(n=Count("products"))
    return render(request, "products/category_list.html", {"categories": cats})


@manager_required
def category_edit(request, pk=None):
    cat = get_object_or_404(ProductCategory, pk=pk) if pk else None
    form = CategoryForm(request.POST or None, instance=cat)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        audit("product.category_save", obj, user=request.user, summary=f"Category {obj} saved")
        return redirect("products:category_list")
    return render(request, "core/form_page.html", {"form": form, "title": "Category"})


# ---- Documents -------------------------------------------------------------------

def document_list(request):
    docs = CompanyDocument.objects.prefetch_related("categories", "products").select_related("uploaded_by")
    return render(request, "products/document_list.html", {"documents": docs})


@editor_required
def document_edit(request, pk=None):
    doc = get_object_or_404(CompanyDocument, pk=pk) if pk else None
    form = DocumentForm(request.POST or None, request.FILES or None, instance=doc)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if obj.approved_for_sending and not (request.user.is_superuser or _is_manager(request.user)):
            obj.approved_for_sending = doc.approved_for_sending if doc else False
            messages.warning(request, "Only a manager can approve documents for sending.")
        if not obj.uploaded_by_id:
            obj.uploaded_by = request.user
        obj.full_clean()
        obj.save()
        form.save_m2m()
        audit("document.save", obj, user=request.user, summary=f"Document {obj} saved")
        messages.success(request, "Document saved.")
        return redirect("products:document_list")
    return render(request, "core/form_page.html", {"form": form, "multipart": True, "title": "Catalogue / document"})


def _is_manager(user):
    from apps.core.permissions import is_manager
    return is_manager(user)


@manager_required
@require_POST
def document_delete(request, pk):
    doc = get_object_or_404(CompanyDocument, pk=pk)
    doc.file.delete(save=False)
    doc.delete()
    audit("document.delete", user=request.user, summary=f"Document {pk} deleted")
    messages.success(request, "Document deleted.")
    return redirect("products:document_list")


# ---- Price lists -----------------------------------------------------------------

def pricelist_list(request):
    lists = PriceList.objects.annotate(n_items=Count("items")).select_related("approved_by")
    return render(request, "products/pricelist_list.html", {"price_lists": lists})


@manager_required
def pricelist_edit(request, pk=None):
    pl = get_object_or_404(PriceList, pk=pk) if pk else None
    if pl and not pl.is_editable:
        messages.error(request, "Approved price lists cannot be edited. Create a new version instead.")
        return redirect("products:pricelist_detail", pk=pk)
    form = PriceListForm(request.POST or None, instance=pl)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if not obj.created_by_id:
            obj.created_by = request.user
        obj.save()
        audit("pricelist.save", obj, user=request.user, summary=f"Price list {obj} saved")
        return redirect("products:pricelist_detail", pk=obj.pk)
    return render(request, "core/form_page.html", {"form": form, "title": "Price list"})


def pricelist_detail(request, pk):
    pl = get_object_or_404(PriceList, pk=pk)
    items = pl.items.select_related("variant__product").order_by("variant__product__name", "variant__size_mm", "min_quantity")
    return render(request, "products/pricelist_detail.html", {
        "pl": pl, "items": items, "item_form": PriceItemForm() if pl.is_editable else None,
    })


@manager_required
@require_POST
def pricelist_item_add(request, pk):
    pl = get_object_or_404(PriceList, pk=pk)
    if not pl.is_editable:
        messages.error(request, "This price list is locked.")
        return redirect("products:pricelist_detail", pk=pk)
    form = PriceItemForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.price_list = pl
        try:
            item.save()
            messages.success(request, "Price added.")
        except IntegrityError:
            messages.error(request, "A price for this size and quantity break already exists in this list.")
    else:
        messages.error(request, "Invalid price: " + "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect("products:pricelist_detail", pk=pk)


@manager_required
@require_POST
def pricelist_item_delete(request, pk):
    item = get_object_or_404(PriceListItem, pk=pk)
    pl_id = item.price_list_id
    try:
        item.delete()
        messages.success(request, "Price removed.")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("products:pricelist_detail", pk=pl_id)


@manager_required
def pricelist_import(request, pk):
    """CSV columns: sku, unit_price, min_quantity (optional, default 1)."""
    pl = get_object_or_404(PriceList, pk=pk)
    if not pl.is_editable:
        messages.error(request, "This price list is locked.")
        return redirect("products:pricelist_detail", pk=pk)
    form = CsvUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            rows = form.read_rows()
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("products:pricelist_import", pk=pk)
        saved, errors = 0, []
        with transaction.atomic():
            for i, row in enumerate(rows, start=2):
                variant = ProductVariant.objects.filter(sku=row.get("sku", "")).first()
                price = parse_decimal(row.get("unit_price", ""))
                min_qty = row.get("min_quantity") or "1"
                if not variant:
                    errors.append(f"Row {i}: unknown SKU '{row.get('sku', '')}'")
                    continue
                if price is None or price <= 0:
                    errors.append(f"Row {i}: invalid unit_price")
                    continue
                if not min_qty.isdigit():
                    errors.append(f"Row {i}: invalid min_quantity")
                    continue
                PriceListItem.objects.update_or_create(
                    price_list=pl, variant=variant, min_quantity=int(min_qty), defaults={"unit_price": price}
                )
                saved += 1
        audit("pricelist.import", pl, user=request.user, summary=f"{saved} prices imported", errors=errors[:50])
        messages.success(request, f"{saved} prices imported, {len(errors)} errors.")
        for e in errors[:10]:
            messages.warning(request, e)
        return redirect("products:pricelist_detail", pk=pk)
    return render(request, "core/form_page.html", {
        "form": form, "multipart": True, "title": f"Import prices into {pl.name}",
        "subtitle": "CSV columns: sku, unit_price, min_quantity (optional)", "submit_label": "Import",
        "download_url": reverse("products:csv_template", args=["prices"]), "download_label": "Download CSV template",
    })


@manager_required
@require_POST
def pricelist_action(request, pk):
    pl = get_object_or_404(PriceList, pk=pk)
    action = request.POST.get("action")
    if action == "approve":
        if not pl.is_editable:
            messages.error(request, "Only draft price lists can be approved.")
        else:
            try:
                pl.approve(request.user)
                audit("pricelist.approve", pl, user=request.user, summary=f"Price list {pl} approved")
                messages.success(request, "Price list approved. Quotations can now use these prices.")
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
    elif action == "archive":
        pl.status = PriceList.Status.ARCHIVED
        pl.save(update_fields=["status"])
        audit("pricelist.archive", pl, user=request.user, summary=f"Price list {pl} archived")
        messages.info(request, "Price list archived.")
    elif action == "new_version":
        with transaction.atomic():
            new = PriceList.objects.create(
                name=f"{pl.name} (new version)", currency=pl.currency, incoterm_basis=pl.incoterm_basis,
                notes=f"Copied from #{pl.pk}", created_by=request.user,
            )
            PriceListItem.objects.bulk_create([
                PriceListItem(price_list=new, variant=i.variant, unit_price=i.unit_price, min_quantity=i.min_quantity)
                for i in pl.items.all()
            ])
        audit("pricelist.new_version", new, user=request.user, summary=f"Draft copy of {pl} created")
        messages.success(request, "Draft copy created. Edit it, then approve it.")
        return redirect("products:pricelist_detail", pk=new.pk)
    return redirect("products:pricelist_detail", pk=pk)


CSV_TEMPLATES = {
    "products": PRODUCT_COLUMNS,
    "sizes": SIZE_COLUMNS,
    "sizes-for-one-product": VARIANT_COLUMNS,
    "prices": PRICE_COLUMNS,
}


def csv_template(request, kind):
    """Header-only CSV (UTF-8 with BOM so Excel opens it correctly)."""
    from django.http import Http404, HttpResponse

    columns = CSV_TEMPLATES.get(kind)
    if columns is None:
        raise Http404("Unknown template")
    resp = HttpResponse("\ufeff" + ",".join(columns) + "\r\n", content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="flotec-{kind}-template.csv"'
    return resp


def _bulk_import(request, *, title, columns, template_kind, importer, action, extra_help=""):
    form = CsvUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            rows = form.read_rows()
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect(request.path)
        created, updated, errors = importer(rows)
        audit(action, user=request.user, summary=f"{created} created, {updated} updated, {len(errors)} errors", errors=errors[:50])
        messages.success(request, f"Import finished: {created} created, {updated} updated, {len(errors)} rows with errors.")
        for e in errors[:15]:
            messages.warning(request, e)
        return redirect("products:product_list")
    return render(request, "core/form_page.html", {
        "form": form, "multipart": True, "title": title, "submit_label": "Import",
        "subtitle": f"CSV columns: {', '.join(columns)}. {extra_help}",
        "download_url": reverse("products:csv_template", args=[template_kind]), "download_label": "Download CSV template",
    })


@editor_required
def product_import(request):
    cats = ", ".join(ProductCategory.objects.values_list("name", flat=True))
    return _bulk_import(request, title="Import products (CSV)", columns=PRODUCT_COLUMNS, template_kind="products",
                        importer=import_products, action="product.import",
                        extra_help=f"Category must be one of: {cats}. Material: PP, PVC, UPVC, CPVC, RPVC, HDPE, LDPE, ABS, OTHER. "
                                   "sample_available / oem_customisable: yes or no. Imported products start as not verified.")


@editor_required
def sizes_import(request):
    return _bulk_import(request, title="Import sizes for all products (CSV)", columns=SIZE_COLUMNS, template_kind="sizes",
                        importer=import_sizes, action="product.sizes_import",
                        extra_help="product_name must match an existing product (add material if two products share a name). SKU must be unique.")
