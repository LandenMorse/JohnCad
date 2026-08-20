from django.urls import path

from . import views

urlpatterns = [
    path('', views.attachment_upload, name='attachment-list'),
    path('attachments/<int:pk>/delete/', views.attachment_delete, name='attachment-delete'),
    path('attachments/<int:pk>/download/', views.attachment_download, name='attachment-download'),
]
