from django.urls import path

from . import two_factor, views

app_name = "core"

urlpatterns = [
    path("settings/", views.settings_home, name="settings_home"),
    path("settings/company/", views.company_profile_edit, name="company_profile"),
    path("settings/system/", views.system_settings_edit, name="system_settings"),
    path("settings/users/", views.user_list, name="user_list"),
    path("settings/users/new/", views.user_create, name="user_create"),
    path("settings/users/<int:pk>/", views.user_edit, name="user_edit"),
    path("settings/users/<int:pk>/reset-2fa/", two_factor.admin_reset, name="user_reset_2fa"),
    path("settings/audit/", views.audit_log, name="audit_log"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/read/", views.notifications_mark_read, name="notifications_read"),
]
