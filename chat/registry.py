"""
Open chat sockets in this process (hardening spec 4.2 and 10.1, socket counting).

Production runs one Daphne process with the in-memory channel layer, so every chat
socket lives in this process and a plain dictionary can count them exactly. The
consumer checks the caps and adds its socket in one call, with no `await` in
between, so two sockets that connect at the same moment cannot both slip under a
cap. A crashed process takes its sockets with it, so no slot can leak the way a
counter in a shared store would.

A move to several processes (a Redis channel layer, more Daphne workers) needs a
shared registry instead: this one only sees its own process.
"""

import itertools
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from syncmily import limits

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenSocket:
    channel_name: str
    user_id: int
    workspace_id: str
    # The session that opened the socket, or None when the scope had no session
    # (only tests build such a scope; the auth middleware needs a session).
    session_key: str | None
    # Rises with every socket, so a fan-out can tell sockets it checked from
    # sockets that opened after it checked.
    serial: int
    # How to reach the consumer from another thread: its event loop and a coroutine
    # function that closes it.
    loop: object = field(default=None, compare=False, repr=False)
    revoke: object = field(default=None, compare=False, repr=False)


_lock = threading.Lock()
_serials = itertools.count(1)
_by_channel = {}
_by_user = defaultdict(set)
_by_family = defaultdict(set)


def claim(
    channel_name, *, user_id, workspace_id, session_key=None, loop=None, revoke=None
):
    """Add a socket if the user and the family are under their caps.

    Returns the OpenSocket, or None when a cap is full (the caller closes with 4429).
    """
    workspace_id = str(workspace_id)
    with _lock:
        if channel_name in _by_channel:
            return _by_channel[channel_name]
        if len(_by_user[user_id]) >= limits.QUOTAS.chat_sockets_per_user:
            return None
        if len(_by_family[workspace_id]) >= limits.QUOTAS.chat_sockets_per_family:
            return None
        socket = OpenSocket(
            channel_name=channel_name,
            user_id=user_id,
            workspace_id=workspace_id,
            session_key=session_key,
            serial=next(_serials),
            loop=loop,
            revoke=revoke,
        )
        _by_channel[channel_name] = socket
        _by_user[user_id].add(channel_name)
        _by_family[workspace_id].add(channel_name)
        return socket


def release(channel_name):
    """Forget a socket. Safe to call more than once."""
    with _lock:
        socket = _by_channel.pop(channel_name, None)
        if socket is None:
            return
        for index, key in (
            (_by_user, socket.user_id),
            (_by_family, socket.workspace_id),
        ):
            names = index.get(key)
            if names is not None:
                names.discard(channel_name)
                if not names:
                    del index[key]


def family_sockets(workspace_id):
    """The open sockets of one family, as a list (a snapshot)."""
    with _lock:
        names = _by_family.get(str(workspace_id), ())
        return [_by_channel[name] for name in names]


def user_sockets(user_id):
    with _lock:
        return [_by_channel[name] for name in _by_user.get(user_id, ())]


def revoke_user_sockets(user_id, session_key=None):
    """Close a person's open chat sockets with 4403, at once.

    With session_key, only the sockets that session opened close (sign-out on one
    device). Safe to call from any thread: the close runs on each socket's own event
    loop. Returns how many sockets were asked to close.
    """
    asked = 0
    for socket in user_sockets(user_id):
        if session_key is not None and socket.session_key != session_key:
            continue
        if socket.loop is None or socket.revoke is None:
            continue
        try:
            socket.loop.call_soon_threadsafe(_start, socket.loop, socket.revoke)
        except RuntimeError:  # the loop has closed, so the socket is gone too
            release(socket.channel_name)
            continue
        asked += 1
    return asked


def _start(loop, revoke):
    loop.create_task(revoke())
