from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from apps.core import views as core_views
from apps.outreach import views as outreach_views
from apps.reports import views as report_views

admin.site.site_header = "Flotec Sales AI – administration"

urlpatterns = [
    path("", report_views.dashboard, name="dashboard"),
    path("accounts/login/", core_views.login_view, name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/password/", auth_views.PasswordChangeView.as_view(), name="password_change"),
    path("accounts/password/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),
    path("healthz", core_views.healthz, name="healthz"),
    path("u/<str:token>/", outreach_views.unsubscribe, name="unsubscribe"),
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path("products/", include("apps.products.urls")),
    path("crm/", include("apps.crm.urls")),
    path("geo/", include("apps.geo.urls")),
    path("research/", include("apps.research.urls")),
    path("agents/", include("apps.agents.urls")),
    path("mailbox/", include("apps.mailbox.urls")),
    path("outreach/", include("apps.outreach.urls")),
    path("followups/", include("apps.followups.urls")),
    path("quotations/", include("apps.quotations.urls")),
    path("reports/", include("apps.reports.urls")),
    # Uploaded files are served only to signed-in users (LoginRequiredMiddleware applies).
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
