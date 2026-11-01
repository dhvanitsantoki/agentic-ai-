from django.urls import path

from . import views

app_name = "products"

urlpatterns = [
    path("", views.product_list, name="product_list"),
    path("new/", views.product_edit, name="product_create"),
    path("<int:pk>/", views.product_detail, name="product_detail"),
    path("<int:pk>/edit/", views.product_edit, name="product_edit"),
    path("<int:pk>/verify/", views.product_verify, name="product_verify"),
    path("<int:pk>/variants/add/", views.variant_add, name="variant_add"),
    path("<int:pk>/variants/import/", views.variant_import, name="variant_import"),
    path("variants/<int:pk>/edit/", views.variant_edit, name="variant_edit"),
    path("variants/<int:pk>/delete/", views.variant_delete, name="variant_delete"),
    path("<int:pk>/images/add/", views.image_add, name="image_add"),
    path("images/<int:pk>/delete/", views.image_delete, name="image_delete"),
    path("categories/", views.category_list, name="category_list"),
    path("categories/new/", views.category_edit, name="category_create"),
    path("categories/<int:pk>/", views.category_edit, name="category_edit"),
    path("documents/", views.document_list, name="document_list"),
    path("documents/new/", views.document_edit, name="document_create"),
    path("documents/<int:pk>/", views.document_edit, name="document_edit"),
    path("documents/<int:pk>/delete/", views.document_delete, name="document_delete"),
    path("prices/", views.pricelist_list, name="pricelist_list"),
    path("prices/new/", views.pricelist_edit, name="pricelist_create"),
    path("prices/<int:pk>/", views.pricelist_detail, name="pricelist_detail"),
    path("prices/<int:pk>/edit/", views.pricelist_edit, name="pricelist_edit"),
    path("prices/<int:pk>/items/add/", views.pricelist_item_add, name="pricelist_item_add"),
    path("prices/items/<int:pk>/delete/", views.pricelist_item_delete, name="pricelist_item_delete"),
    path("prices/<int:pk>/import/", views.pricelist_import, name="pricelist_import"),
    path("prices/<int:pk>/action/", views.pricelist_action, name="pricelist_action"),
]
