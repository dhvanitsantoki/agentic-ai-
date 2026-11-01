from django.urls import path

from . import views

app_name = "followups"

urlpatterns = [
    path("", views.enrollment_list, name="enrollment_list"),
    path("<int:pk>/action/", views.enrollment_action, name="enrollment_action"),
    path("sequences/new/", views.sequence_edit, name="sequence_create"),
    path("sequences/<int:pk>/", views.sequence_edit, name="sequence_edit"),
]
