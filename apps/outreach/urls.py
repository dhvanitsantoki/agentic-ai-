from django.urls import path

from . import views

app_name = "outreach"

urlpatterns = [
    path("approvals/", views.approval_queue, name="approval_queue"),
    path("emails/", views.email_list, name="email_list"),
    path("emails/compose/", views.compose, name="compose"),
    path("emails/<int:pk>/", views.email_detail, name="email_detail"),
    path("emails/<int:pk>/edit/", views.email_edit, name="email_edit"),
    path("emails/<int:pk>/decide/", views.email_decide, name="email_decide"),
    path("draft/company/<int:pk>/", views.draft_for_company, name="draft_for_company"),
    path("templates/", views.template_list, name="template_list"),
    path("templates/<int:pk>/", views.template_edit, name="template_edit"),
    path("suppression/", views.suppression_list, name="suppression_list"),
    path("suppression/add/", views.suppression_add, name="suppression_add"),
    path("suppression/<int:pk>/remove/", views.suppression_remove, name="suppression_remove"),
]
