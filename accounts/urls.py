from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path('login/', views.CmsLoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('signup/', views.signup, name='signup'),
    path('classes/', views.classroom_list, name='classroom-list'),
    path('classes/new/', views.classroom_create, name='classroom-create'),
    path('classes/join/', views.classroom_join, name='classroom-join'),
    path('classes/<int:pk>/', views.classroom_detail, name='classroom-detail'),
    path('classes/<int:pk>/codes/new/', views.code_create, name='classroom-code-create'),
    path('classes/<int:pk>/codes/<int:code_pk>/revoke/', views.code_revoke, name='classroom-code-revoke'),
    path('classes/<int:pk>/students/<int:user_pk>/remove/', views.student_remove, name='classroom-student-remove'),
    path('classes/<int:pk>/teachers/add/', views.teacher_add, name='classroom-teacher-add'),
    path('classes/<int:pk>/teachers/<int:user_pk>/remove/', views.teacher_remove, name='classroom-teacher-remove'),
]
