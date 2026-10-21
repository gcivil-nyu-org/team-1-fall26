
from django.urls import path

from .consumers import HouseholdChatConsumer

websocket_urlpatterns = [
    path(
        "ws/chat/<uuid:workspace_id>/",
        HouseholdChatConsumer.as_asgi(),
    ),
]
