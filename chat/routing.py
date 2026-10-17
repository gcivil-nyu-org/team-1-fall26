from django.urls import path

from .consumers import HouseholdChatConsumer

websocket_urlpatterns = [
    path(
        "ws/chat/",
        HouseholdChatConsumer.as_asgi(),
    ),
]
