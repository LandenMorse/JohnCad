from django.urls import path

from . import views

urlpatterns = [
    path('attachments/<int:pk>/export/', views.attachment_export, name='attachment-export'),
    path('queue/', views.queue, name='queue'),
]
