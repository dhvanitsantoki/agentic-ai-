from django.urls import path

from . import views

app_name = "mailbox"

urlpatterns = [
    path("accounts/", views.account_list, name="account_list"),
    path("accounts/<int:pk>/", views.account_edit, name="account_edit"),
    path("accounts/<int:pk>/disconnect/", views.account_disconnect, name="account_disconnect"),
    path("oauth/start/<str:provider>/", views.oauth_start, name="oauth_start"),
    path("oauth/callback/<str:provider>/", views.oauth_callback, name="oauth_callback"),
    path("sync/", views.sync_now, name="sync_now"),
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/<int:pk>/", views.inbound_detail, name="inbound_detail"),
    path("inbox/<int:pk>/action/", views.inbound_action, name="inbound_action"),
]
