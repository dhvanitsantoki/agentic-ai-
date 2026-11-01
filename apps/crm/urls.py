from django.urls import path

from . import views

app_name = "crm"

urlpatterns = [
    path("pipeline/", views.pipeline, name="pipeline"),
    path("companies/", views.company_list, name="company_list"),
    path("companies/new/", views.company_edit, name="company_create"),
    path("companies/import/", views.company_import, name="company_import"),
    path("companies/<int:pk>/", views.company_detail, name="company_detail"),
    path("companies/<int:pk>/edit/", views.company_edit, name="company_edit"),
    path("companies/<int:pk>/stage/", views.company_stage, name="company_stage"),
    path("companies/<int:pk>/flag/", views.company_flag, name="company_flag"),
    path("companies/<int:pk>/delete/", views.company_delete, name="company_delete"),
    path("companies/<int:pk>/activity/", views.activity_add, name="activity_add"),
    path("companies/<int:company_pk>/contacts/new/", views.contact_edit, name="contact_create"),
    path("companies/<int:company_pk>/requirements/new/", views.requirement_edit, name="requirement_create"),
    path("requirements/<int:pk>/", views.requirement_edit, name="requirement_edit"),
    path("contacts/", views.contact_list, name="contact_list"),
    path("contacts/export.csv", views.export_contacts_csv, name="contact_export"),
    path("contacts/<int:pk>/", views.contact_edit, name="contact_edit"),
    path("contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
    path("tasks/", views.task_list, name="task_list"),
    path("tasks/new/", views.task_create, name="task_create"),
    path("tasks/<int:pk>/toggle/", views.task_done, name="task_done"),
]
