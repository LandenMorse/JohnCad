from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path('login/', views.CmsLoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('signup/', views.signup, name='signup'),
    path('', views.attachment_upload, name='attachment-list'),
    path('attachments/<int:pk>/delete/', views.attachment_delete, name='attachment-delete'),
]
