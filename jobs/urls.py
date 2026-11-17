from django.urls import path

from . import views

urlpatterns = [
    path('attachments/<int:pk>/export/', views.attachment_export, name='attachment-export'),
    path('queue/', views.queue, name='queue'),
    path('queue/release-next/', views.queue_release_next, name='queue-release-next'),
    path('review/', views.review, name='job-review'),
    path('review/<int:pk>/approve/', views.review_approve, name='job-review-approve'),
    path('review/<int:pk>/reject/', views.review_reject, name='job-review-reject'),
    path('review/<int:pk>/delete/', views.review_delete, name='job-review-delete'),
    path('review/<int:pk>/retry-dispatch/', views.review_retry_dispatch, name='job-review-retry-dispatch'),
    path('printers/', views.printers, name='printers'),
    path('printers/<int:pk>/', views.printer_detail, name='printer-detail'),
    path('printers/tickets/<int:pk>/cancel/', views.printer_ticket_cancel, name='printer-ticket-cancel'),
    path('printers/templates/upload/', views.template_upload, name='template-upload'),
    path('printers/templates/<int:pk>/download/', views.template_download, name='template-download'),
    path('printers/templates/<int:pk>/delete/', views.template_delete, name='template-delete'),
]
