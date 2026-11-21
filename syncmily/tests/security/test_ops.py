"""Ops-lane PoCs: weak points beyond security (111 production pitfalls).

Lane: ops. Branch security/audit-2026-11-18 (production commit a37d73d).

Naming
    test_poc_OPS_<id>_<slug>   asserts the SECURE / RESILIENT behaviour, so it FAILS
                               while the weak point exists.
    test_control_<slug>        passes today; pins behaviour that must not regress.

Nothing here leaves the process: Groq, Google (Routes, OAuth) and open routing
providers are mocked and their calls counted. Run:

    POSTGRES_DB=syncmily_sec_ops manage.py test syncmily.tests.security.test_ops
"""

import unittest
import asyncio
import os
import re
import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

import requests
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import OperationalError, connection, transaction
from django.db.models import NOT_PROVIDED
from django.db.migrations.loader import MigrationLoader
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from calendars.models import CalendarConnection
from checklists.models import Checklist
from events import upstream
from events.models import Event
from syncmily.asgi import application

from .ops_helpers import PASSWORD, build_household, count_queries, login

User = get_user_model()
REPO = Path(settings.BASE_DIR)


def _read(relative):
    return (REPO / relative).read_text()


# ---------------------------------------------------------------------------
# Query counts (pitfalls 26, 27): the pages must not grow with the data.
# ---------------------------------------------------------------------------


class QueryCountScalingTests(TestCase):
    """Query count must be identical at 1x, 3x and 9x data (no N+1)."""

    PAGES = {
        "dashboard_parent": ("parents", lambda h: reverse("accounts:dashboard")),
        "dashboard_kid": ("kids", lambda h: reverse("accounts:dashboard")),
        "day": (
            "parents",
            lambda h: reverse("events:workspace_list", args=[h["ws"].pk]) + "?view=day",
        ),
        "week": (
            "parents",
            lambda h: reverse("events:workspace_list", args=[h["ws"].pk])
            + "?view=week",
        ),
        "upcoming": (
            "parents",
            lambda h: reverse("events:workspace_list", args=[h["ws"].pk])
            + "?view=upcoming",
        ),
        "event_detail": (
            "parents",
            lambda h: reverse("events:detail", args=[h["events"][0].pk]),
        ),
        "chat_page": (
            "parents",
            lambda h: reverse("chat:workspace_chat", args=[h["ws"].pk]),
        ),
        "chat_history_json": (
            "parents",
            lambda h: reverse("chat:messages", args=[h["ws"].pk]),
        ),
        "checklists": (
            "parents",
            lambda h: reverse("checklists:list", args=[h["ws"].pk]),
        ),
    }

    def test_control_query_counts_flat_with_data_size(self):
        counts = {}
        for scale in (1, 3, 9):
            household = build_household(f"q{scale}", scale)
            for name, (role, url_for) in self.PAGES.items():
                client = login(household[role][0])
                response, n, _ = count_queries(client, url_for(household))
                self.assertEqual(response.status_code, 200, name)
                counts.setdefault(name, {})[scale] = n
        for name, by_scale in counts.items():
            self.assertEqual(
                len(set(by_scale.values())), 1, f"N+1 on {name}: {by_scale}"
            )

    # Open follow-up: profile and trim the context processors to 15 queries a page. Audit REPORT.md (h).
    @unittest.expectedFailure
    def test_poc_OPS_QPAGE_001_page_query_budget(self):
        """OPS-QPAGE-001 (pitfalls 26/27/66): a plain page view should not need more
        than 15 queries; the app shell plus the page cost 19-29, all on one thread,
        each a round trip to RDS (no persistent connection, see OPS-DB-001)."""
        household = build_household("budget", 1)
        worst = {}
        for name, (role, url_for) in self.PAGES.items():
            client = login(household[role][0])
            _, n, _ = count_queries(client, url_for(household))
            worst[name] = n
        over = {k: v for k, v in worst.items() if v > 15}
        self.assertFalse(over, f"pages over 15 queries: {over}")


# ---------------------------------------------------------------------------
# OPS-KIDQ-001: kid visibility filter NOT IN (every attendance row)  (25, 26, 92)
# ---------------------------------------------------------------------------


class KidVisibilityQueryScaleTests(TestCase):
    ATTENDANCE_ROWS = 400_000

    def test_poc_OPS_KIDQ_001_kid_page_query_survives_large_attendance_table(self):
        """OPS-KIDQ-001: events.access.family_events builds, for a kid,
        NOT (pk IN (SELECT event_id FROM events_event_participants)) over the WHOLE
        table (no workspace filter). Past ~300k attendance rows Postgres stops
        hashing that subquery (work_mem 4MB) and rescans it per candidate event:
        a 400-event household drops from 45 ms to 7-22 s per query, on every kid
        page, for everyone. Secure behaviour: the query stays under 1 s."""
        from events.access import family_events

        parent = User.objects.create_user("big-parent", password=PASSWORD)
        kid = User.objects.create_user("big-kid", password=PASSWORD)
        ws = Workspace.objects.create(name="Big", primary_admin=parent)
        now = timezone.now()
        Membership.objects.create(
            workspace=ws, user=kid, role="kid", invited_by=parent, accepted_at=now
        )
        base = now.replace(minute=0, second=0, microsecond=0)
        Event.objects.bulk_create(
            Event(
                created_by=parent,
                workspace=ws,
                title=f"T{i}",
                starts_at=base + timedelta(hours=6 * i),
                ends_at=base + timedelta(hours=6 * i, minutes=45),
            )
            for i in range(400)
        )
        mine = list(Event.objects.filter(workspace=ws)[:200])
        Event.participants.through.objects.bulk_create(
            Event.participants.through(event_id=e.pk, user_id=parent.pk) for e in mine
        )
        # Filler: other families' attendance rows (400k) in one SQL statement.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO events_event (submission_id, created_by_id, workspace_id,
                    requires_transport, title, starts_at, ends_at, timezone_name,
                    location, transport_mode, is_tentative, source, external_id,
                    created_at, updated_at)
                SELECT gen_random_uuid(), %s, %s, false, 'filler', now(),
                    now() + interval '1 hour', 'America/New_York', '', 'transit',
                    false, 'syncmily', '', now(), now()
                FROM generate_series(1, %s)
                """,
                [parent.pk, ws.pk, self.ATTENDANCE_ROWS // 2],
            )
            cursor.execute(
                """
                INSERT INTO events_event_participants (event_id, user_id)
                SELECT e.id, u FROM events_event e, unnest(ARRAY[%s, %s]) u
                WHERE e.title = 'filler'
                """,
                [parent.pk, kid.pk],
            )
            cursor.execute("ANALYZE events_event_participants")
            cursor.execute("ANALYZE events_event")
            cursor.execute("SET LOCAL statement_timeout = 20000")
        started = time.monotonic()
        try:
            with transaction.atomic():
                list(
                    family_events(kid)
                    .filter(workspace=ws, ends_at__gt=now, title__startswith="T")
                    .order_by("starts_at", "pk")[:5]
                )
            elapsed = time.monotonic() - started
        except OperationalError:
            elapsed = time.monotonic() - started
            self.fail(
                f"kid events query hit the 20 s statement timeout ({elapsed:.1f}s)"
            )
        self.assertLess(elapsed, 1.0, f"kid events query took {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# OPS-CHAT-001: authorisation lookups grow with the number of sockets (27, 44)
# ---------------------------------------------------------------------------


class ChatFanoutTests(TransactionTestCase):
    def _run(self, n_sockets):
        admin = User.objects.create_user(f"cf{n_sockets}-admin", password=PASSWORD)
        ws = Workspace.objects.create(name=f"cf{n_sockets}", primary_admin=admin)
        users = [admin]
        for i in range(n_sockets - 1):
            u = User.objects.create_user(f"cf{n_sockets}-u{i}", password=PASSWORD)
            Membership.objects.create(
                workspace=ws,
                user=u,
                role="parent",
                invited_by=admin,
                accepted_at=timezone.now(),
            )
            users.append(u)
        cookies = []
        for u in users:
            c = Client()
            c.login(username=u.username, password=PASSWORD)
            cookies.append(c.cookies["sessionid"].value)

        def sock(cookie):
            return WebsocketCommunicator(
                application,
                f"/ws/chat/{ws.id}/",
                headers=[
                    (b"host", b"localhost"),
                    (b"origin", b"http://localhost:8000"),
                    (b"cookie", f"sessionid={cookie}".encode()),
                ],
            )

        from chat import consumers, services

        async def main():
            comms = [sock(c) for c in cookies]
            for comm in comms:
                ok, _ = await comm.connect()
                assert ok
            with mock.patch.object(
                consumers,
                "get_authorized_workspace",
                wraps=consumers.get_authorized_workspace,
            ) as a, mock.patch.object(
                services,
                "get_authorized_workspace",
                wraps=services.get_authorized_workspace,
            ) as b:
                await comms[0].send_json_to(
                    {"type": "chat_message", "content": "hello", "priority": "normal"}
                )
                for comm in comms:
                    await asyncio.wait_for(comm.receive_json_from(), 5)
                calls = a.call_count + b.call_count
            for comm in comms:
                await comm.disconnect()
            return calls

        with override_settings(RATELIMIT_ENABLED=False):
            return async_to_sync(main)()

    def test_poc_OPS_CHAT_001_auth_lookups_do_not_scale_with_sockets(self):
        """OPS-CHAT-001 (27/44): each message re-authorises per recipient socket
        (workspace get + membership EXISTS = 2 queries each, all through the single
        database thread). Secure behaviour: lookups per message do not grow with the
        number of connected household members."""
        small, large = self._run(1), self._run(6)
        self.assertEqual(
            small,
            large,
            f"auth lookups per message: 1 socket={small}, 6 sockets={large}",
        )


# ---------------------------------------------------------------------------
# OPS-LIST-001: unbounded checklist list (26)
# ---------------------------------------------------------------------------


class UnboundedChecklistTests(TestCase):
    def test_poc_OPS_LIST_001_checklist_list_is_paginated(self):
        """OPS-LIST-001 (26): checklists:list renders EVERY checklist of the family;
        creating is capped at 60/min per user and nothing caps the total. Secure
        behaviour: a page shows at most 100."""
        household = build_household("cl", 1)
        Checklist.objects.bulk_create(
            Checklist(
                workspace=household["ws"],
                title=f"OpsBulk{i:04d}",
                created_by=household["admin"],
            )
            for i in range(500)
        )
        client = login(household["parents"][0])
        response = client.get(reverse("checklists:list", args=[household["ws"].pk]))
        shown = len(set(re.findall(rb"OpsBulk\d{4}", response.content)))
        self.assertLessEqual(
            shown, 100, f"{shown} of 500 checklists rendered in one page"
        )


# ---------------------------------------------------------------------------
# OPS-RL-001: google_sync has no rate limit (1, 10)
# ---------------------------------------------------------------------------


class GoogleSyncRateLimitTests(TestCase):
    @override_settings(RATELIMIT_ENABLED=True)
    def test_poc_OPS_RL_001_google_sync_is_rate_limited(self):
        """OPS-RL-001 (1): POST /calendars/google/sync/ triggers a Google Calendar
        fetch per call with no limit (nginx limits only login, signup, scan-ocr).
        Secure behaviour: 429 well before 100 imports in a row."""
        from calendars.sync import SyncResult

        cache.clear()
        household = build_household("rl", 1)
        parent = household["parents"][0]
        CalendarConnection.objects.create(
            user=parent, workspace=household["ws"], provider="google"
        )
        client = login(parent)
        with mock.patch(
            "calendars.views.sync_connection", return_value=SyncResult()
        ) as sync:
            statuses = [
                client.post(reverse("calendars:google_sync")).status_code
                for _ in range(100)
            ]
        self.assertIn(429, statuses, f"{sync.call_count} Google fetches in 100 clicks")


# ---------------------------------------------------------------------------
# OPS-BRK-001 / OPS-TMO-001: breakers and timeouts (9, 10, 11, 94, 95)
# ---------------------------------------------------------------------------


class UpstreamResilienceTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_poc_OPS_BRK_001_groq_has_a_circuit_breaker(self):
        """OPS-BRK-001 (10): only Google Routes, Transitous and the MTA use
        events.upstream; Groq is called on every request even while it is down.
        Secure behaviour: after 3 failures the next calls are not sent."""
        from events.ai_suggestions import (
            EventSuggestionError,
            generate_event_suggestion,
        )

        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-not-real"}), mock.patch(
            "events.ai_suggestions.requests.post",
            side_effect=requests.ConnectionError("down"),
        ) as post:
            for i in range(8):
                with self.assertRaises(EventSuggestionError):
                    generate_event_suggestion(
                        f"dentist tomorrow at 3 number {i}",
                        timezone_name="America/New_York",
                    )
        self.assertLessEqual(
            post.call_count, upstream.OUTAGE_THRESHOLD, post.call_count
        )

    def test_poc_OPS_TMO_001_google_oauth_token_exchange_has_a_timeout(self):
        """OPS-TMO-001 (11/95): flow.fetch_token in calendars/google.py is called
        without a timeout (requests default: wait forever) inside the sign-in
        callback view. Secure behaviour: every outbound call carries a timeout."""
        from calendars import google

        seen = []

        def fake_request(self, method, url, **kwargs):
            seen.append(kwargs.get("timeout", "absent"))
            raise requests.ConnectionError("stop")

        with mock.patch("requests.Session.request", fake_request):
            with self.assertRaises(requests.ConnectionError):
                google.verify_login("https://example.test/cb", "code", "verifier")
        self.assertTrue(seen, "no outbound call captured")
        self.assertTrue(
            all(isinstance(t, (int, float, tuple)) and t for t in seen),
            f"timeouts passed to requests: {seen}",
        )

    def test_poc_OPS_TMO_002_id_token_verification_timeout_is_short(self):
        """OPS-TMO-002 (11/95): id_token.verify_oauth2_token(..., Request(), ...) uses
        google-auth's default 120 s timeout to fetch Google's certs, longer than
        nginx's 60 s. Secure behaviour: default transport timeout <= 10 s."""
        from google.auth.transport import requests as gar

        self.assertLessEqual(gar._DEFAULT_TIMEOUT, 10)

    def test_poc_OPS_TMO_003_upstream_timeout_is_a_total_deadline(self):
        """OPS-TMO-003 (11/94): events.upstream.TIMEOUT = (3.05, 8) is requests'
        per-read timeout, not a total deadline: a service that keeps sending one
        byte every few seconds never trips it. Live, one trip card held its request
        171 s (nginx gives up at 60 s) with a 1 byte/s upstream; here a body that
        takes 18 s to arrive must not hold a call longer than 12 s."""
        import http.server
        import socketserver

        class Drip(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"x" * 60
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.3)
                except OSError:
                    pass

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Drip)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started = time.monotonic()
        try:
            try:
                requests.get(
                    f"http://127.0.0.1:{server.server_address[1]}/",
                    timeout=upstream.TIMEOUT,
                )
            except requests.RequestException:
                pass
            elapsed = time.monotonic() - started
        finally:
            server.shutdown()
            server.server_close()
        self.assertLess(
            elapsed, 12, f"call held for {elapsed:.1f}s with a (3.05, 8) timeout"
        )

    @override_settings(GOOGLE_MAPS_API_KEY="test-not-real")  # production has a key set
    def test_poc_OPS_TRAVEL_001_event_save_has_a_total_upstream_budget(self):
        """OPS-TRAVEL-001 (11/94/10): edit/create event POST runs find_travel_checks
        inline: for every participant, up to 2 neighbour pairs, each one a serial
        routing call (Valhalla/OSRM timeout 6-15 s, no breaker, no cache). With the
        routing provider timing out, the worst case is participants x 2 x 15 s.
        Secure behaviour: the summed timeouts a single request can spend stay under
        30 s (half of nginx's 60 s)."""
        household = build_household("trv", 2)
        admin, ws = household["admin"], household["ws"]
        people = [admin, *household["parents"], *household["kids"]]
        day = (timezone.now() + timedelta(days=3)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        main = Event.objects.create(
            created_by=admin,
            workspace=ws,
            title="Main",
            starts_at=day,
            ends_at=day + timedelta(hours=1),
            location="Main St",
            latitude=40.70,
            longitude=-74.0,
            transport_mode="walking",
        )
        main.participants.set(people)
        for i, person in enumerate(people):
            before = Event.objects.create(
                created_by=admin,
                workspace=ws,
                title=f"B{i}",
                starts_at=day - timedelta(hours=3),
                ends_at=day - timedelta(hours=2),
                location="A",
                latitude=40.71 + i * 0.001,
                longitude=-74.01,
            )
            after = Event.objects.create(
                created_by=admin,
                workspace=ws,
                title=f"A{i}",
                starts_at=day + timedelta(hours=3),
                ends_at=day + timedelta(hours=4),
                location="B",
                latitude=40.72 + i * 0.001,
                longitude=-74.02,
            )
            before.participants.set([person])
            after.participants.set([person])
        spent = []

        def stalled(url, **kwargs):
            timeout = kwargs.get("timeout")
            spent.append(max(timeout) if isinstance(timeout, tuple) else timeout)
            raise requests.ReadTimeout("stalled")

        client = login(admin)
        local = timezone.localtime(day)
        data = {
            "title": "Main",
            "date": local.date().isoformat(),
            "start_time": local.strftime("%H:%M"),
            "end_time": (local + timedelta(hours=1)).strftime("%H:%M"),
            "timezone": settings.TIME_ZONE,
            "location": "Main St",
            "latitude": "40.70",
            "longitude": "-74.0",
            "transport_mode": "walking",
            "participants": [p.pk for p in people],
            "action": "preview",
        }
        with mock.patch(
            "events.routing.requests.post", side_effect=stalled
        ), mock.patch("events.routing.requests.get", side_effect=stalled):
            response = client.post(reverse("events:edit", args=[main.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(spent, "no routing calls were made")
        self.assertLessEqual(
            sum(spent),
            30,
            f"{len(spent)} serial routing calls could hold one request for {sum(spent)} s",
        )


# ---------------------------------------------------------------------------
# OPS-CACHE-001: rate-limit / outage flags share a 300-entry LocMem (2, 50)
# ---------------------------------------------------------------------------


class SharedCacheEvictionTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_poc_OPS_CACHE_001_outage_flag_survives_route_cache_churn(self):
        """OPS-CACHE-001 (2/50): settings has no CACHES, so one per-process LocMem
        (MAX_ENTRIES 300) holds the rate-limit counters, the upstream-down flags and
        every cached trip. Route churn evicts the flags. Secure behaviour: the flag
        set first is still there after 400 unrelated cache.set calls."""
        upstream.record_failure(
            "google-routes", requests.ConnectionError(), "k", threshold=1
        )
        self.assertTrue(upstream.is_cooling_off("google-routes"))
        for i in range(400):
            cache.set(f"trip-google:{i}", [{"route": i}], 300)
        self.assertTrue(upstream.is_cooling_off("google-routes"))

    def test_poc_OPS_CACHE_002_login_rate_counter_survives_cache_churn(self):
        """OPS-CACHE-002 (1/2/50): the per-IP login counter (rl:login:*) is evicted
        by the same churn, resetting the limit. Secure behaviour: the counter keeps
        counting."""
        from syncmily.ratelimit import get_count, hit

        with override_settings(RATELIMIT_ENABLED=True):  # tests run with it off
            for _ in range(5):
                hit("login", "ip:203.0.113.9", 150, 600)
        for i in range(400):
            cache.set(f"trip-google:{i}", [{"route": i}], 300)
        self.assertEqual(get_count("login", "ip:203.0.113.9", 600), 5)

    def test_control_caches_setting_is_default_locmem(self):
        self.assertEqual(
            settings.CACHES["default"]["BACKEND"],
            "django.core.cache.backends.locmem.LocMemCache",
        )


# ---------------------------------------------------------------------------
# OPS-SYNC-001 / 002: cron overlap and retry-forever (13, 21, 39)
# ---------------------------------------------------------------------------


class CalendarSyncTests(TransactionTestCase):
    def _connection(self, prefix="sync"):
        admin = User.objects.create_user(f"{prefix}-admin", password=PASSWORD)
        ws = Workspace.objects.create(name=prefix, primary_admin=admin)
        return CalendarConnection.objects.create(
            user=admin,
            workspace=ws,
            provider="google",
            account_email="a@example.test",
            access_token="a",
            refresh_token="r",
            scopes="x",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_poc_OPS_SYNC_001_overlapping_syncs_do_not_fail(self):
        """OPS-SYNC-001 (21/39): the 15-minute systemd timer and the Import button
        both call sync_connection with no lock. Both read 'existing' before either
        inserts, so the second INSERT hits unique_imported_event_per_connection and
        its whole transaction rolls back (the web path then records sync_error=failed
        for a sync that actually succeeded elsewhere). Secure behaviour: both runs
        finish without an exception."""
        from calendars import sync

        connection_row = self._connection("race")
        now = timezone.now()
        items = [
            {
                "id": "evt-1",
                "summary": "Soccer",
                "status": "confirmed",
                "start": {"dateTime": (now + timedelta(days=1)).isoformat()},
                "end": {"dateTime": (now + timedelta(days=1, hours=1)).isoformat()},
            }
        ]
        barrier = threading.Barrier(2, timeout=10)
        real_times = sync._event_times

        def waiting_times(item, tz):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return real_times(item, tz)

        errors = []

        def run():
            try:
                row = CalendarConnection.objects.get(pk=connection_row.pk)
                with mock.patch.object(
                    sync, "_fetch_items", return_value=(items, "UTC")
                ), mock.patch.object(
                    sync, "_event_times", waiting_times
                ), mock.patch.object(
                    sync, "_service"
                ), mock.patch.object(
                    sync,
                    "_credentials",
                    return_value=mock.Mock(token=None, expiry=None),
                ):
                    sync.sync_connection(row, now=now)
            except Exception as error:  # noqa: BLE001
                errors.append(f"{type(error).__name__}: {str(error)[:200]}")
            finally:
                from django.db import connection as db

                db.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [], f"overlapping syncs failed with {errors}")

    def test_poc_OPS_SYNC_002_revoked_connection_is_not_retried_forever(self):
        """OPS-SYNC-002 (13): a connection whose Google access was revoked is still
        selected by every 15-minute run (auto_sync stays on), so Google is called
        96 times a day for a token that can never work. Secure behaviour: after
        access_revoked the next run does not call Google for that connection."""
        from google.auth.exceptions import RefreshError

        self._connection("revoked")
        with mock.patch(
            "calendars.management.commands.sync_calendars.sync_connection",
            side_effect=RefreshError("revoked"),
        ) as sync:
            for _ in range(3):
                try:
                    call_command("sync_calendars", stderr=mock.MagicMock())
                except Exception:  # CommandError: failures reported
                    pass
        self.assertEqual(sync.call_count, 1, f"Google called {sync.call_count}x")


# ---------------------------------------------------------------------------
# OPS-LOCK-001: lost update on concurrent edits (36)
# ---------------------------------------------------------------------------


class LostUpdateTests(TestCase):
    def test_poc_OPS_LOCK_001_stale_edit_does_not_overwrite_newer_edit(self):
        """OPS-LOCK-001 (36): edit_event saves whatever the form carried, with no
        version or updated_at comparison. Two parents open the same event; the first
        renames it, the second (stale form) moves the location and silently reverts
        the rename. Secure behaviour: the second save is refused or merged."""
        household = build_household("lock", 2)
        event = household["events"][0]
        a, b = login(household["parents"][0]), login(household["parents"][1])
        local = timezone.localtime(event.starts_at)

        def payload(title, location):
            return {
                "title": title,
                "date": local.date().isoformat(),
                "start_time": local.strftime("%H:%M"),
                "end_time": timezone.localtime(event.ends_at).strftime("%H:%M"),
                "timezone": event.timezone_name,
                "location": location,
                "transport_mode": event.transport_mode,
                "requires_transport": "on",
                "dropoff_driver": event.dropoff_driver_id or "",
                "pickup_driver": event.pickup_driver_id or "",
                "participants": [p.pk for p in event.participants.all()],
                "action": "save",
            }

        url = reverse("events:edit", args=[event.pk])
        self.assertEqual(a.get(url).status_code, 200)
        self.assertEqual(b.get(url).status_code, 200)  # both hold the original
        first = a.post(url, payload("Renamed by A", event.location))
        self.assertEqual(first.status_code, 302, first.content[:300])
        second = b.post(url, payload(event.title, "New place by B"))
        event.refresh_from_db()
        self.assertTrue(
            event.title == "Renamed by A" or second.status_code != 302,
            f"B's stale save won: title={event.title!r}, status={second.status_code}",
        )


class DoubleSubmitTests(TestCase):
    def test_poc_OPS_IDEM_001_checklist_create_is_idempotent(self):
        """OPS-IDEM-001 (14): event creation carries a signed token + unique
        submission_id, checklist creation has none, so a double click or a retried
        POST creates two lists. Secure behaviour: one row for two identical posts."""
        household = build_household("idem", 1)
        client = login(household["parents"][0])
        url = reverse("checklists:create", args=[household["ws"].pk])
        for _ in range(2):
            client.post(url, {"title": "Packing list"})
        self.assertEqual(
            Checklist.objects.filter(
                workspace=household["ws"], title="Packing list"
            ).count(),
            1,
        )


# ---------------------------------------------------------------------------
# OPS-DUP-001: no DB uniqueness on email (14, 39)
# ---------------------------------------------------------------------------


class DuplicateEmailTests(TransactionTestCase):
    def test_poc_OPS_DUP_001_database_rejects_duplicate_email(self):
        """OPS-DUP-001 (14/39): email uniqueness is only a form check; auth_user.email
        has no unique index, so two simultaneous signups (or a signup racing a Google
        first login) can both pass the check. Secure behaviour: the database refuses
        a second row with the same email (case-insensitive)."""
        from django.db import IntegrityError

        User.objects.create_user(
            "dup-one", email="Same@Example.test", password=PASSWORD
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    "dup-two", email="same@example.test", password=PASSWORD
                )


# ---------------------------------------------------------------------------
# OPS-DB-00x: database connection settings (11, 28, 73, 95)
# ---------------------------------------------------------------------------


class DatabaseSettingsTests(TestCase):
    def test_poc_OPS_DB_001_connections_are_reused(self):
        """OPS-DB-001 (28): no CONN_MAX_AGE, so every request opens and closes a
        Postgres connection (TLS handshake to RDS). Secure behaviour: connections are
        reused through a bounded pool. Under ASGI, per-thread persistent connections
        (CONN_MAX_AGE > 0) piled up until Postgres refused new ones (live round 2, L2),
        so the pool replaces them and Django requires CONN_MAX_AGE = 0 with it."""
        db = settings.DATABASES["default"]
        pool = db.get("OPTIONS", {}).get("pool")
        self.assertIsInstance(pool, dict)
        self.assertGreater(pool["max_size"], 0)
        self.assertLessEqual(pool["max_size"], 50)
        self.assertLessEqual(pool["timeout"], 10)
        self.assertEqual(db.get("CONN_MAX_AGE"), 0)

    def test_control_OPS_DB_001_database_exhaustion_is_503_with_retry_after(self):
        """Live round 2, L2: when Postgres refuses a connection (too many clients) or
        the pool wait expires, the answer is a 503 with Retry-After, not a 500. The
        error can come from middleware (the session lookup), so it goes through
        handler500, which sees it in sys.exc_info()."""
        from django.db.utils import OperationalError
        from django.test import RequestFactory

        from syncmily import errors

        factory = RequestFactory()
        for accept, content_type in (
            ("text/html", "text/html"),
            ("application/json", "application/json"),
        ):
            request = factory.get("/accounts/dashboard/", HTTP_ACCEPT=accept)
            try:
                raise OperationalError("FATAL: sorry, too many clients already")
            except OperationalError:
                response = errors.handler500(request)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response["Retry-After"], "5")
            self.assertIn(content_type, response["Content-Type"])
            self.assertNotIn("too many clients", response.content.decode())
        request = factory.get("/accounts/dashboard/")
        try:
            raise RuntimeError("other")
        except RuntimeError:
            self.assertEqual(errors.handler500(request).status_code, 500)

    def test_poc_OPS_DB_002_connect_timeout_is_set(self):
        """OPS-DB-002 (11/95): no connect_timeout in OPTIONS: a black-holed RDS
        endpoint blocks libpq until the OS gives up (minutes). Secure behaviour: a
        connect_timeout of 10 s or less."""
        timeout = (
            settings.DATABASES["default"].get("OPTIONS", {}).get("connect_timeout")
        )
        self.assertTrue(timeout and timeout <= 10, f"connect_timeout={timeout}")

    def test_poc_OPS_DB_003_statement_timeout_is_set(self):
        """OPS-DB-003 (11/81): no statement_timeout: one runaway query (see
        OPS-KIDQ-001) holds its request thread for as long as it runs. Secure
        behaviour: a server-side statement_timeout under 30 s."""
        options = settings.DATABASES["default"].get("OPTIONS", {}).get("options", "")
        self.assertTrue(
            "statement_timeout" in options, f"DATABASES OPTIONS options={options!r}"
        )

    def test_poc_OPS_DB_004_tls_to_database_is_verified(self):
        """OPS-DB-004 (73): no sslmode: libpq 'prefer' encrypts but never verifies the
        RDS certificate. Secure behaviour: sslmode verify-full (or verify-ca)."""
        mode = settings.DATABASES["default"].get("OPTIONS", {}).get("sslmode")
        self.assertIn(mode, ("verify-full", "verify-ca"))


# ---------------------------------------------------------------------------
# OPS-HEALTH-001 / OPS-HDR-001 / OPS-AI-001 (56, 48, 51)
# ---------------------------------------------------------------------------


class ReadinessAndHeadersTests(TestCase):
    def test_control_healthz_answers_without_database(self):
        with mock.patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=OperationalError("db down"),
        ):
            self.assertEqual(Client().get("/healthz/").status_code, 200)

    def test_poc_OPS_HEALTH_001_readiness_endpoint_checks_database(self):
        """OPS-HEALTH-001 (56): /healthz/ is answered by middleware before any DB
        call, so it stays 200 when Postgres is down; no /readyz/ exists. Secure
        behaviour: a readiness URL that returns 503 while the DB is unreachable."""
        with mock.patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=OperationalError("db down"),
        ):
            response = Client().get("/readyz/")
        self.assertEqual(response.status_code, 503)

    def test_poc_OPS_HDR_001_authenticated_html_is_not_cacheable(self):
        """OPS-HDR-001 (48): dashboard, chat and event pages send no Cache-Control,
        so a shared cache or a browser back button can keep one person's schedule.
        Secure behaviour: private/no-store on signed-in HTML."""
        household = build_household("hdr", 1)
        client = login(household["parents"][0])
        for url in (
            reverse("accounts:dashboard"),
            reverse("chat:workspace_chat", args=[household["ws"].pk]),
            reverse("events:detail", args=[household["events"][0].pk]),
        ):
            response = client.get(url)
            header = response.headers.get("Cache-Control", "")
            self.assertTrue(
                "no-store" in header or "private" in header, f"{url}: {header!r}"
            )

    def test_poc_OPS_AI_001_ai_panel_hidden_without_api_key(self):
        """OPS-AI-001 (51): GROQ_API_KEY is not set in production, yet the 'Describe
        your event' AI panel always renders and every use logs an ERROR. Secure
        behaviour: no panel when the feature is unconfigured."""
        household = build_household("ai", 1)
        client = login(household["parents"][0])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            response = client.get(
                reverse("events:workspace_create", args=[household["ws"].pk])
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Describe your event", response.content)


# ---------------------------------------------------------------------------
# OPS-MIG-00x: migrations (52, 54, 55, 81, 82)
# ---------------------------------------------------------------------------

LAST_TEN = [
    ("calendars", "0004_connection_sync_error"),
    ("calendars", "0003_connection_auto_sync"),
    ("accounts", "0008_workspace_home"),
    ("events", "0013_trip_start"),
    ("chat", "0004_chatmessage_priority"),
    ("accounts", "0007_day_briefing"),
    ("accounts", "0006_palette_names"),
    ("events", "0012_event_dropoff_driver_event_pickup_driver_and_more"),
    ("events", "0011_event_is_tentative"),
    ("checklists", "0001_initial"),
]


class MigrationSafetyTests(TestCase):
    def test_control_no_model_drift(self):
        call_command("makemigrations", check=True, dry_run=True, verbosity=0)

    def test_control_one_leaf_per_app(self):
        loader = MigrationLoader(connection)
        leaves = {}
        for app, name in loader.graph.leaf_nodes():
            leaves.setdefault(app, []).append(name)
        for app in ("accounts", "events", "chat", "calendars", "checklists"):
            self.assertEqual(len(leaves[app]), 1, leaves[app])

    def test_poc_OPS_MIG_001_new_not_null_columns_have_a_database_default(self):
        """OPS-MIG-001 (52/54): migrate runs BEFORE the new code goes live
        (.ebextensions 01_migrate, container_commands) while the OLD code keeps
        serving. Django drops the temporary column default after AddField, so a
        NOT NULL column added with a python-only default makes the old release's
        INSERT fail until the swap. Secure behaviour: every NOT NULL column added by
        the last ten migrations has a database default (db_default)."""
        loader = MigrationLoader(connection)
        bad = []
        for key in LAST_TEN:
            migration = loader.disk_migrations[key]
            for operation in migration.operations:
                if operation.__class__.__name__ != "AddField":
                    continue
                field = operation.field
                if field.null or field.many_to_many or field.primary_key:
                    continue
                if field.db_default is NOT_PROVIDED:
                    bad.append(f"{key[0]}.{operation.model_name}.{operation.name}")
        self.assertFalse(bad, f"NOT NULL without db_default: {bad}")

    def test_poc_OPS_MIG_002_old_release_insert_works_on_new_schema(self):
        """OPS-MIG-002 (52/54): the previous release's INSERT into
        calendars_calendarconnection (no auto_sync / sync_error columns) must still
        succeed against the new schema. It fails with a NOT NULL violation."""
        from django.db import IntegrityError

        admin = User.objects.create_user("mig-admin", password=PASSWORD)
        try:
            with connection.cursor() as cursor, transaction.atomic():
                cursor.execute(
                    """
                    INSERT INTO calendars_calendarconnection
                        (user_id, provider, account_email, calendar_id, access_token,
                         refresh_token, scopes, sync_token, created_at, updated_at)
                    VALUES (%s, 'google', '', 'primary', '', '', '', '', now(), now())
                    """,
                    [admin.pk],
                )
        except IntegrityError as error:
            self.fail(f"previous release's INSERT rejected: {str(error)[:90]}")

    def test_poc_OPS_MIG_003_ci_blocks_unmade_migrations(self):
        """OPS-MIG-003 (6/82): ci.yml never runs makemigrations --check, so a model
        change without its migration can be deployed. Secure behaviour: CI has it."""
        self.assertTrue(
            "makemigrations" in _read(".github/workflows/ci.yml"),
            "ci.yml has no makemigrations --check",
        )

    def test_poc_OPS_RB_001_deploy_takes_a_snapshot_before_migrating(self):
        """OPS-RB-001 (55/83/84): the only rollback is `migrate <app> <prev>` or a
        manual RDS snapshot (docs/deploy-elastic-beanstalk.md). Rehearsed on a seeded
        database, migrating the newest ten migrations back and forward again keeps
        the schema but loses TripStart rows, drivers, workspace home, chat priority,
        auto_sync/sync_error and every chat message's workspace link. ci.yml deploys
        main on every green push and never snapshots. Secure behaviour: the deploy
        job runs rds create-db-snapshot before update-environment."""
        self.assertTrue(
            "create-db-snapshot" in _read(".github/workflows/ci.yml"),
            "ci.yml deploy job takes no RDS snapshot",
        )

    def test_poc_OPS_MIG_004_migrate_has_lock_timeout(self):
        """OPS-MIG-004 (81): migrate runs in the deploy with no lock_timeout, so a
        blocked ALTER TABLE queues behind a long query and stalls every request.
        Secure behaviour: lock_timeout set in the database options or the command."""
        options = settings.DATABASES["default"].get("OPTIONS", {}).get("options", "")
        config = _read(".ebextensions/01_django.config")
        self.assertTrue(
            "lock_timeout" in options or "lock_timeout" in config,
            "no lock_timeout anywhere",
        )


# ---------------------------------------------------------------------------
# OPS-CFG-00x: deploy and operations configuration read from the repo (21, 31,
# 59, 93, 103)
# ---------------------------------------------------------------------------


class DeployConfigTests(TestCase):
    def test_poc_OPS_CFG_001_sync_timer_unit_has_a_timeout(self):
        """OPS-CFG-001 (21/95): the oneshot syncmily-calendar-sync.service has no
        TimeoutStartSec (systemd disables the timeout for Type=oneshot), so one
        stuck Google call keeps the unit 'activating' forever and the timer never
        fires again. Secure behaviour: TimeoutStartSec set."""
        script = next((REPO / ".platform/hooks/postdeploy").glob("*calendar_sync*"))
        self.assertTrue(
            "TimeoutStartSec" in script.read_text(),
            "no TimeoutStartSec in the sync unit",
        )

    def test_poc_OPS_CFG_002_expired_sessions_are_purged(self):
        """OPS-CFG-002 (31): sessions are database-backed (no SESSION_ENGINE) and
        nothing runs clearsessions; django_session grows forever. Secure behaviour:
        a scheduled clearsessions in the deploy hooks."""
        text = "".join(
            p.read_text()
            for base in (".platform", ".ebextensions")
            for p in (REPO / base).rglob("*")
            if p.is_file()
        )
        self.assertTrue(
            "clearsessions" in text, "no clearsessions job in .platform/.ebextensions"
        )

    def test_poc_OPS_CFG_003_axes_access_log_has_retention(self):
        """OPS-CFG-003 (31): django-axes writes an AccessLog row per login attempt
        (AXES_DISABLE_ACCESS_LOG unset) and nothing runs axes_reset_logs. Secure
        behaviour: access log disabled, or a retention job exists."""
        text = "".join(
            p.read_text()
            for base in (".platform", ".ebextensions")
            for p in (REPO / base).rglob("*")
            if p.is_file()
        )
        self.assertTrue(
            getattr(settings, "AXES_DISABLE_ACCESS_LOG", False)
            or "axes_reset_logs" in text
        )

    def test_poc_OPS_CFG_004_nginx_logs_request_time(self):
        """OPS-CFG-004 (62/64/93): nginx writes the stock 'main' log_format, so there
        is no request_time / upstream_response_time and no way to compute p95/p99 or
        a latency SLO. Secure behaviour: a custom log_format containing them."""
        text = _read(".platform/hooks/postdeploy/03_https.sh")
        self.assertTrue(
            re.search(r"log_format[^;]*request_time", text),
            "no request_time in any log_format",
        )

    def test_poc_OPS_CFG_005_application_logging_is_configured(self):
        """OPS-CFG-005 (59/66): settings has no LOGGING and there is no request id, so
        logger.warning calls (trip failures with exc_info, upstream errors) go through
        Django's defaults with no structure, user or request correlation. Secure
        behaviour: LOGGING configured."""
        self.assertTrue(getattr(settings, "LOGGING", None))

    def test_poc_OPS_CFG_006_prerelease_pin_in_production(self):
        """OPS-CFG-006 (103/108): requirements.in pins twisted to a release candidate;
        26.10.0 final shipped on 2026-10-30. Secure behaviour: no pre-release pin."""
        text = _read("requirements.in")
        match = re.search(r"(?im)^twisted==\S*(rc|a|b|dev)\d", text)
        self.assertIsNone(match, match.group(0) if match else "")

    def test_poc_OPS_CFG_007_certbot_is_pinned(self):
        """OPS-CFG-007 (103/108): 03_https.sh pip-installs certbot unpinned on every
        fresh instance. Secure behaviour: a pinned version."""
        text = _read(".platform/hooks/postdeploy/03_https.sh")
        self.assertTrue(
            re.search(r"pip install[^\n]*certbot==", text),
            "certbot is installed unpinned",
        )
