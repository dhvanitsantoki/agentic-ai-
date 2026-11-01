from django.urls import path

from . import views

app_name = "geo"

urlpatterns = [
    path("", views.market_overview, name="market_overview"),
    path("country/<str:iso2>/", views.country_detail, name="country_detail"),
    path("country/<str:iso2>/update/", views.country_update, name="country_update"),
    path("reports/<int:pk>/", views.report_detail, name="report_detail"),
    path("reports/<int:pk>/review/", views.report_review, name="report_review"),
]
