from django.urls import path

from . import views

app_name = "agents"

urlpatterns = [
    path("", views.control, name="control"),
    path("state/", views.set_state, name="set_state"),
    path("mode/", views.set_mode, name="set_mode"),
    path("schedule/<int:pk>/", views.task_edit, name="task_edit"),
    path("schedule/<int:pk>/run/", views.task_run_now, name="task_run_now"),
    path("jobs/", views.job_list, name="job_list"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/retry/", views.job_retry, name="job_retry"),
    path("runs/", views.run_log, name="run_log"),
]
