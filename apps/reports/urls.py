from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.report_list, name="list"),
    path("generate/", views.report_generate, name="generate"),
    path("<int:pk>/", views.report_detail, name="detail"),
    path("<int:pk>/export/<str:fmt>/", views.report_export, name="export"),
]
