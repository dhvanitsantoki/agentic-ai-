from django.urls import path

from . import views

app_name = "research"

urlpatterns = [
    path("runs/", views.run_list, name="run_list"),
    path("runs/new/", views.run_create, name="run_create"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("runs/<int:pk>/cancel/", views.run_cancel, name="run_cancel"),
    path("review/", views.review_queue, name="review_queue"),
    path("review/<int:pk>/", views.finding_review, name="finding_review"),
    path("deep-dive/<int:pk>/", views.deep_dive, name="deep_dive"),
    path("country/<str:iso2>/", views.country_report, name="country_report"),
]
