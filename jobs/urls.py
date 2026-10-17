from django.urls import path

from . import views

urlpatterns = [
    path('attachments/<int:pk>/export/', views.attachment_export, name='attachment-export'),
    path('queue/', views.queue, name='queue'),
    path('queue/release-next/', views.queue_release_next, name='queue-release-next'),
    path('review/', views.review, name='job-review'),
    path('review/<int:pk>/approve/', views.review_approve, name='job-review-approve'),
    path('review/<int:pk>/reject/', views.review_reject, name='job-review-reject'),
    path('review/<int:pk>/retry-dispatch/', views.review_retry_dispatch, name='job-review-retry-dispatch'),
]
