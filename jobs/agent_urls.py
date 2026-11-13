from django.urls import path

from . import agent_api

urlpatterns = [
    path('poll/', agent_api.poll, name='agent-poll'),
    path('jobs/<int:pk>/claim/', agent_api.claim, name='agent-claim'),
    path('jobs/<int:pk>/file/', agent_api.download, name='agent-download'),
    path('jobs/<int:pk>/dispatched/', agent_api.dispatched, name='agent-dispatched'),
    path('jobs/<int:pk>/dispatch-failed/', agent_api.dispatch_failed, name='agent-dispatch-failed'),
    path('jobs/<int:pk>/released/', agent_api.released, name='agent-released'),
    path('jobs/<int:pk>/release-failed/', agent_api.release_failed, name='agent-release-failed'),
    path('jobs/<int:pk>/event/', agent_api.event, name='agent-event'),
    path('printer-tickets/<int:pk>/claim/', agent_api.printer_ticket_claim, name='agent-printer-ticket-claim'),
    path('printer-tickets/<int:pk>/done/', agent_api.printer_ticket_done, name='agent-printer-ticket-done'),
    path('printer-tickets/<int:pk>/failed/', agent_api.printer_ticket_failed, name='agent-printer-ticket-failed'),
]
