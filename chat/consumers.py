from channels.generic.websocket import AsyncJsonWebsocketConsumer


class HouseholdChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        # Connections remain disabled until household
        # membership authorization is implemented.
        await self.close(code=4403)
