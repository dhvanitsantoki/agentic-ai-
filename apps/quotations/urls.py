from django.urls import path

from . import views

app_name = "quotations"

urlpatterns = [
    path("", views.quotation_list, name="list"),
    path("new/company/<int:pk>/", views.create_for_company, name="create_for_company"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/terms/", views.update_terms, name="update_terms"),
    path("<int:pk>/lines/add/", views.line_add, name="line_add"),
    path("lines/<int:pk>/", views.line_action, name="line_action"),
    path("<int:pk>/action/", views.quotation_action, name="action"),
    path("<int:pk>/pdf/", views.pdf, name="pdf"),
]
