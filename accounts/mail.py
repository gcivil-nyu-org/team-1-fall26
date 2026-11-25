"""Sending account email without making the answer slower for real accounts.

A view that mails only when an account exists (password reset, neutral sign-up)
would answer more slowly for a real address, which tells a stranger the address has
an account (finding reset-timing-enumeration). These helpers send from a worker
thread and let the view wait a fixed time on every path.
"""

import logging
import threading
import time

from django.db import close_old_connections, connections, transaction

logger = logging.getLogger(__name__)

# Every path of a view that may send mail answers after this long, at the soonest.
# The wait also gives a fast mail backend time to finish before the response.
SEND_WAIT_SECONDS = 0.05


def _deliver(message):
    close_old_connections()
    try:
        message.send()
    except Exception:
        # The mail server refused it; the visitor sees the same answer either way.
        logger.exception("An account email could not be sent")
    finally:
        connections.close_all()


def send_in_background(message):
    """Start sending one message on a worker thread and return the thread."""
    worker = threading.Thread(target=_deliver, args=(message,), daemon=True)
    worker.start()
    return worker


def send_after_commit(message):
    """Send once the current transaction commits, so a link never points at a row
    that was rolled back."""
    transaction.on_commit(lambda: send_in_background(message))


class FixedDelay:
    """Make every path through a block take the same time:

        with FixedDelay() as delay:
            ...
            delay.threads.extend(form.sent)

    On exit it waits for the listed send threads, then sleeps until
    SEND_WAIT_SECONDS have passed since the start. A slow mail server never holds
    the answer longer than that.
    """

    def __init__(self, seconds=SEND_WAIT_SECONDS):
        self.seconds = seconds
        self.threads = []

    def __enter__(self):
        self.deadline = time.monotonic() + self.seconds
        return self

    def __exit__(self, *exc):
        for worker in self.threads:
            worker.join(max(self.deadline - time.monotonic(), 0))
        remaining = self.deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        return False
