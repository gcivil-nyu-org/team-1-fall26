"""Shared helper for socket tests.

The chat consumer accepts a refused socket and closes it at once with a code, so the
browser sees the code (a close before accept shows only a bare 403). A test that asks
"was this socket let in?" must therefore look for that close right after the connect.
"""


async def connect_and_check(communicator):
    """Connect; return (open, code). A socket that closes at once counts as refused."""
    connected, code = await communicator.connect()
    if not connected:
        return False, code
    if await communicator.receive_nothing(timeout=0.2):
        return True, code
    message = await communicator.receive_output(timeout=1)
    if message.get("type") == "websocket.close":
        return False, message.get("code")
    return True, code
