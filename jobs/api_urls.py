from django.urls import path

from . import api_views

urlpatterns = [
    path('jobs/queue/', api_views.queue, name='api-jobs-queue'),
    path('jobs/<int:pk>/gcode/', api_views.download_gcode, name='api-job-gcode'),
    path('jobs/<int:pk>/working/', api_views.mark_working, name='api-job-working'),
    path('jobs/<int:pk>/finished/', api_views.mark_finished, name='api-job-finished'),
]
