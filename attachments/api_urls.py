from django.urls import path

from . import api_views

urlpatterns = [
    path('queue/', api_views.queue, name='api-queue'),
    path('attachments/<int:pk>/download/', api_views.download, name='api-attachment-download'),
    path('attachments/<int:pk>/fetched/', api_views.mark_fetched, name='api-attachment-fetched'),
]
