from django.urls import path

from . import api_views

urlpatterns = [
    path('bambuddy/webhook/', api_views.bambuddy_webhook, name='api-bambuddy-webhook'),
]
