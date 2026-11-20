"""Authorization matrix and PoCs for the authz lane (audit branch security/audit-2026-11-18).

Every test asserts the SECURE behaviour, so a failing test is a confirmed bug.
Tests named test_matrix_* walk one route across ten roles; tests named
test_poc_AUTHZ_* pin one finding; tests named test_control_* pass today and guard
behaviour that must stay secure.

Roles
    anonymous        no session
    kid              accepted kid of household A
    parent           accepted (non-primary) parent of household A
    admin            primary admin of household A (has no Membership row)
    other_parent     accepted parent of household B
    other_admin      primary admin of household B
    pending_inactive invited, never confirmed (inactive account), session forced
    pending_active   defense in depth: ACTIVE account whose Membership.accepted_at is NULL
    google_only      Google sign-in account, unusable password, no household
    no_household     ordinary signed-up account, no household

No request leaves the process: Google Places, Routes, MTA, Google OAuth, Groq and
OCR are mocked and their call counts asserted.
"""

import hashlib
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import serializers, signing
from django.core.cache import cache
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import GoogleIdentity, MemberColor, Membership, Workspace
from accounts.services import make_invite_token
from calendars.models import CalendarConnection
from chat.models import ChatMessage
from checklists.models import Checklist, ChecklistItem
from events.models import Event, TripStart

PASSWORD = "Authz-Matrix-2026!"
BACKEND = "django.contrib.auth.backends.ModelBackend"

HOME_LAT, HOME_LNG = 40.689201, -73.944201
HOME_TEXT = "742 Evergreen Terrace, Brooklyn"

ROLES = [
    "anonymous",
    "kid",
    "parent",
    "admin",
    "other_parent",
    "other_admin",
    "pending_inactive",
    "pending_active",
    "google_only",
    "no_household",
]
FAMILY = {"kid", "parent", "admin"}  # household A, any role
CARE = {"parent", "admin"}  # household A caregivers
ADMIN = {"admin"}
KID = {"kid"}
SIGNED_IN = set(ROLES) - {"anonymous", "pending_inactive"}
EVERYONE = set(ROLES)
NOBODY = set()


def snapshot():
    """A hash of every table a request could change."""
    User = get_user_model()
    digest = hashlib.sha256()
    for model in (
        User,
        Workspace,
        Membership,
        MemberColor,
        GoogleIdentity,
        Event,
        TripStart,
        Checklist,
        ChecklistItem,
        CalendarConnection,
        ChatMessage,
    ):
        data = serializers.serialize("json", model.objects.order_by("pk"))
        if model is User:
            # Logins write last_login and the session hash; those are not tenant data.
            data = serializers.serialize(
                "json", model.objects.order_by("pk"), fields=("username", "is_active")
            )
        digest.update(data.encode())
    return digest.hexdigest()


def outcome(response):
    """deny: the server refused. ok: it served or accepted the request. error: 5xx."""
    status = response.status_code
    if status >= 500:
        return "error"
    if status in (401, 403, 404):
        return "deny"
    if status == 302 and "/accounts/login/" in response["Location"]:
        return "deny"
    if status in (400, 405, 429):
        return "bad"
    return "ok"


class AuthzBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        now = timezone.now()

        def user(name, **extra):
            return User.objects.create_user(
                username=name,
                password=PASSWORD,
                email=f"{name.lower()}@example.test",
                **extra,
            )

        def member(workspace, who, role, admin, accepted=True):
            return Membership.objects.create(
                workspace=workspace,
                user=who,
                role=role,
                invited_by=admin,
                accepted_at=now if accepted else None,
            )

        cls.users = {
            "admin": user("AdminA"),
            "parent": user("ParentA"),
            "kid": user("KidA"),
            "other_admin": user("AdminB"),
            "other_parent": user("ParentB"),
            "other_kid": user("KidB"),
            "pending_inactive": user("PendingInactive", is_active=False),
            "pending_active": user("PendingActive"),
            "google_only": user("GoogleOnly"),
            "no_household": user("Loner"),
        }
        u = cls.users
        u["google_only"].set_unusable_password()
        u["google_only"].save()
        GoogleIdentity.objects.create(
            user=u["google_only"], subject="authz-google-sub", email="g@example.test"
        )

        cls.ws_a = Workspace.objects.create(
            name="Household A",
            primary_admin=u["admin"],
            home_location=HOME_TEXT,
            home_latitude=HOME_LAT,
            home_longitude=HOME_LNG,
        )
        cls.ws_b = Workspace.objects.create(
            name="Household B", primary_admin=u["other_admin"]
        )
        member(cls.ws_a, u["parent"], Membership.Role.PARENT, u["admin"])
        cls.kid_membership = member(cls.ws_a, u["kid"], Membership.Role.KID, u["admin"])
        cls.pending_membership = member(
            cls.ws_a,
            u["pending_inactive"],
            Membership.Role.PARENT,
            u["admin"],
            accepted=False,
        )
        member(
            cls.ws_a,
            u["pending_active"],
            Membership.Role.KID,
            u["admin"],
            accepted=False,
        )
        member(cls.ws_b, u["other_parent"], Membership.Role.PARENT, u["other_admin"])
        cls.kid_b_membership = member(
            cls.ws_b, u["other_kid"], Membership.Role.KID, u["other_admin"]
        )

        soon = now + timedelta(days=2)

        def event(ws, title, by, people=(), **extra):
            ev = Event.objects.create(
                created_by=by,
                workspace=ws,
                title=title,
                starts_at=soon,
                ends_at=soon + timedelta(hours=1),
                latitude=40.7506,
                longitude=-73.9935,
                location="Madison Square Garden",
                transport_mode=Event.TransportMode.TRANSIT,
                **extra,
            )
            ev.participants.set(people)
            return ev

        cls.ev_family = event(cls.ws_a, "A family dinner", u["admin"])
        cls.ev_parents = event(
            cls.ws_a, "A parents only", u["admin"], people=[u["admin"], u["parent"]]
        )
        cls.ev_kid = event(cls.ws_a, "A kid practice", u["admin"], people=[u["kid"]])
        cls.ev_b = event(cls.ws_b, "B family dinner", u["other_admin"])
        cls.ev_b_parents = event(
            cls.ws_b, "B parents only", u["other_admin"], people=[u["other_admin"]]
        )

        # parent's own Google calendar, imported into household A
        cls.conn = CalendarConnection.objects.create(
            user=u["parent"],
            workspace=cls.ws_a,
            provider=CalendarConnection.Provider.GOOGLE,
            account_email="parent@gmail.example",
        )
        cls.ev_google = Event.objects.create(
            created_by=u["parent"],
            workspace=cls.ws_a,
            title="A parent private work meeting",
            starts_at=soon,
            ends_at=soon + timedelta(hours=1),
            source=Event.Source.GOOGLE,
            calendar_connection=cls.conn,
            external_id="g-1",
        )

        cls.cl_a = Checklist.objects.create(
            workspace=cls.ws_a,
            title="A checklist",
            created_by=u["admin"],
            event=cls.ev_family,
        )
        cls.item_a = ChecklistItem.objects.create(
            checklist=cls.cl_a, description="A item"
        )
        cls.cl_b = Checklist.objects.create(
            workspace=cls.ws_b,
            title="B checklist",
            created_by=u["other_admin"],
            event=cls.ev_b,
        )
        cls.item_b = ChecklistItem.objects.create(
            checklist=cls.cl_b, description="B item"
        )
        cls.msg_a = ChatMessage.objects.create(
            workspace=cls.ws_a, sender=u["admin"], content="A private chat"
        )
        cls.msg_b = ChatMessage.objects.create(
            workspace=cls.ws_b, sender=u["other_admin"], content="B private chat"
        )

    def setUp(self):
        cache.clear()
        patches = {
            "trip_options": mock.patch(
                "events.trip.trip_options", return_value=("transitous", [])
            ),
            "scan": mock.patch(
                "events.views.scan_event_image",
                return_value={"status": "ok", "event": {"title": "x"}},
            ),
            "ocr_available": mock.patch(
                "events.views.ocr_available", return_value=True
            ),
            "places_enabled": mock.patch("events.places.enabled", return_value=True),
            "places_suggest": mock.patch(
                "events.places.suggest",
                return_value=[{"id": "p", "main": "m", "secondary": ""}],
            ),
            "places_details": mock.patch(
                "events.places.details",
                return_value={"id": "p", "lat": 1.0, "lng": 2.0},
            ),
            "suggestion": mock.patch(
                "events.views.generate_event_suggestion",
                return_value={"title": "t", "missing_fields": []},
            ),
            "g_auth": mock.patch(
                "calendars.google.authorization_url",
                return_value=("https://accounts.example.invalid/auth", "st", "cv"),
            ),
            "g_login": mock.patch(
                "calendars.google.login_authorization_url",
                return_value=("https://accounts.example.invalid/login", "st", "cv"),
            ),
            "g_revoke": mock.patch("calendars.google.revoke", return_value=True),
            "g_sync": mock.patch(
                "calendars.views.sync_connection", return_value="0 added"
            ),
            "g_enabled": mock.patch(
                "accounts.views.google_sign_in_enabled", return_value=True
            ),
        }
        self.mocks = {}
        for name, patch in patches.items():
            self.mocks[name] = patch.start()
            self.addCleanup(patch.stop)
        self.settings_patch = self.settings(
            GOOGLE_CLIENT_ID="authz-test-id", GOOGLE_CLIENT_SECRET="authz-test-secret"
        )
        self.settings_patch.enable()
        self.addCleanup(self.settings_patch.disable)

        self.clients = {"anonymous": Client()}
        for role in ROLES[1:]:
            client = Client()
            client.force_login(self.users[role], backend=BACKEND)
            self.clients[role] = client

    def login(self, role):
        return self.clients[role]

    def isolated(self, role, method, url, data=None, **extra):
        """Send one request, return (response, db_changed), then roll the change back."""
        with transaction.atomic():
            before = snapshot()
            client = self.clients[role]
            response = getattr(client, method)(url, data or {}, **extra)
            changed = snapshot() != before
            transaction.set_rollback(True)
        return response, changed


# --------------------------------------------------------------------------------------
# The matrix: one route, ten roles. `allowed` is who may succeed; everyone else must be
# refused AND must leave the database untouched.
# --------------------------------------------------------------------------------------


def _routes(t):
    """(slug, method, url, data, allowed roles). Built lazily because ids are per-run."""
    a = t.ws_a.pk
    rows = [
        (
            "root",
            "get",
            "/",
            None,
            EVERYONE - {"anonymous", "pending_inactive"}
            | {"anonymous", "pending_inactive"},
        ),
        ("event_list", "get", reverse("events:list"), None, SIGNED_IN),
        (
            "choose_workspace",
            "get",
            reverse("events:choose_workspace"),
            None,
            SIGNED_IN,
        ),
        (
            "event_list_ws",
            "get",
            reverse("events:workspace_list", args=[a]),
            None,
            FAMILY,
        ),
        (
            "event_list_ws_day",
            "get",
            reverse("events:workspace_list", args=[a]) + "?view=day",
            None,
            FAMILY,
        ),
        (
            "create_event_form",
            "get",
            reverse("events:workspace_create", args=[a]),
            None,
            CARE,
        ),
        (
            "create_event_save",
            "post",
            reverse("events:workspace_create", args=[a]),
            {"action": "save", "title": ""},
            CARE,
        ),
        (
            "create_event_ai",
            "post",
            reverse("events:workspace_create", args=[a]),
            {"action": "generate_suggestion", "description": "soccer on friday at 5"},
            CARE,
        ),
        (
            "scan_ocr",
            "post",
            reverse("events:scan_ocr", args=[a]),
            {"timezone": "America/New_York"},
            CARE,
        ),
        (
            "member_colors",
            "post",
            reverse("events:member_colors", args=[a]),
            {f"color-{t.users['admin'].pk}": "#123456"},
            CARE,
        ),
        ("timezones", "get", reverse("events:timezones"), None, EVERYONE),
        # members of ANY household legitimately search places (kids use the trip card box)
        (
            "place_suggestions",
            "post",
            reverse("events:place_suggestions"),
            {"q": "Brooklyn"},
            FAMILY | {"other_parent", "other_admin"},
        ),
        (
            "place_details",
            "post",
            reverse("events:place_details"),
            {"id": "ChIJabcdefghijklmnop"},
            FAMILY | {"other_parent", "other_admin"},
        ),
        (
            "event_detail",
            "get",
            reverse("events:detail", args=[t.ev_family.pk]),
            None,
            FAMILY,
        ),
        (
            "event_detail_drivers",
            "post",
            reverse("events:detail", args=[t.ev_family.pk]),
            {"requires_transport": "on", "dropoff_driver": t.users["parent"].pk},
            CARE,
        ),
        (
            "event_edit_get",
            "get",
            reverse("events:edit", args=[t.ev_family.pk]),
            None,
            CARE,
        ),
        (
            "event_edit_post",
            "post",
            reverse("events:edit", args=[t.ev_family.pk]),
            {"title": ""},
            CARE,
        ),
        (
            "event_delete_get",
            "get",
            reverse("events:delete", args=[t.ev_family.pk]),
            None,
            CARE,
        ),
        (
            "event_delete_post",
            "post",
            reverse("events:delete", args=[t.ev_family.pk]),
            {"action": "delete"},
            CARE,
        ),
        (
            "event_transport",
            "post",
            reverse("events:set_transport_mode", args=[t.ev_family.pk]),
            {"transport_mode": "walking"},
            CARE,
        ),
        (
            "event_trip_get",
            "get",
            reverse("events:trip", args=[t.ev_family.pk]),
            None,
            FAMILY,
        ),
        (
            "event_trip_json",
            "get",
            reverse("events:trip", args=[t.ev_family.pk]) + "?format=json",
            None,
            FAMILY,
        ),
        (
            "event_trip_post",
            "post",
            reverse("events:trip", args=[t.ev_family.pk]),
            {"mode": "home"},
            FAMILY,
        ),
        (
            "event_time",
            "post",
            reverse("events:change_time", args=[t.ev_family.pk]),
            {
                "starts_date": "2031-01-01",
                "starts_time": "10:00",
                "ends_date": "2031-01-01",
                "ends_time": "11:00",
            },
            CARE,
        ),
        ("chat_home", "get", reverse("chat:home"), None, SIGNED_IN),
        (
            "chat_workspace",
            "get",
            reverse("chat:workspace_chat", args=[a]),
            None,
            FAMILY,
        ),
        ("chat_messages", "get", reverse("chat:messages", args=[a]), None, FAMILY),
        (
            "create_workspace_get",
            "get",
            reverse("accounts:create_workspace"),
            None,
            SIGNED_IN,
        ),
        (
            "workspace_detail",
            "get",
            reverse("accounts:workspace_detail", args=[a]),
            None,
            ADMIN,
        ),
        (
            "manage_members_get",
            "get",
            reverse("accounts:manage_members", args=[a]),
            None,
            ADMIN,
        ),
        (
            "manage_members_invite",
            "post",
            reverse("accounts:manage_members", args=[a]),
            {"role": "kid", "email": "newkid@example.test"},
            ADMIN,
        ),
        (
            "resend_invite",
            "post",
            reverse("accounts:resend_invite", args=[a, t.pending_membership.pk]),
            None,
            ADMIN,
        ),
        (
            "remove_member",
            "post",
            reverse("accounts:remove_member", args=[a, t.kid_membership.pk]),
            None,
            ADMIN,
        ),
        ("dashboard", "get", reverse("accounts:dashboard"), None, SIGNED_IN),
        ("day_seen", "post", reverse("accounts:day_seen"), None, SIGNED_IN),
        ("profile", "get", reverse("accounts:profile"), None, SIGNED_IN),
        (
            "google_connect_login",
            "post",
            reverse("accounts:google_connect"),
            None,
            SIGNED_IN,
        ),
        (
            "set_palette",
            "post",
            reverse("accounts:set_palette", args=[a]),
            {"palette": "bold"},
            CARE,
        ),
        (
            "family_home_get",
            "get",
            reverse("accounts:family_home", args=[a]),
            None,
            CARE,
        ),
        (
            "family_home_post",
            "post",
            reverse("accounts:family_home", args=[a]),
            {
                "action": "save",
                "location": "1 Main St",
                "latitude": "40.1",
                "longitude": "-73.1",
            },
            CARE,
        ),
        (
            "family_home_clear",
            "post",
            reverse("accounts:family_home", args=[a]),
            {"action": "clear"},
            CARE,
        ),
        (
            "set_my_palette",
            "post",
            reverse("accounts:set_my_palette", args=[a]),
            {"palette": "bold"},
            KID,
        ),
        ("two_step_setup", "get", reverse("accounts:two_step_setup"), None, SIGNED_IN),
        (
            "two_step_disable",
            "post",
            reverse("accounts:two_step_disable"),
            None,
            SIGNED_IN,
        ),
        (
            "two_step_codes",
            "post",
            reverse("accounts:two_step_backup_codes"),
            None,
            SIGNED_IN,
        ),
        (
            "cal_connect",
            "post",
            reverse("calendars:google_connect", args=[a]),
            None,
            ADMIN,
        ),
        # only the account that owns the connection (the parent) may touch it
        ("cal_sync", "post", reverse("calendars:google_sync"), None, {"parent"}),
        (
            "cal_auto_sync",
            "post",
            reverse("calendars:google_auto_sync"),
            {"auto_sync": "off"},
            {"parent"},
        ),
        (
            "cal_disconnect",
            "post",
            reverse("calendars:google_disconnect"),
            None,
            {"parent"},
        ),
        ("checklist_list", "get", reverse("checklists:list", args=[a]), None, FAMILY),
        (
            "checklist_create_get",
            "get",
            reverse("checklists:create", args=[a]),
            None,
            CARE,
        ),
        (
            "checklist_create_post",
            "post",
            reverse("checklists:create", args=[a]),
            {"title": "New list"},
            CARE,
        ),
        (
            "checklist_detail",
            "get",
            reverse("checklists:detail", args=[t.cl_a.pk]),
            None,
            FAMILY,
        ),
        (
            "checklist_edit_get",
            "get",
            reverse("checklists:edit", args=[t.cl_a.pk]),
            None,
            CARE,
        ),
        (
            "checklist_edit_post",
            "post",
            reverse("checklists:edit", args=[t.cl_a.pk]),
            {"title": "Renamed"},
            CARE,
        ),
        (
            "checklist_delete",
            "post",
            reverse("checklists:delete", args=[t.cl_a.pk]),
            None,
            CARE,
        ),
        (
            "item_add",
            "post",
            reverse("checklists:item_add", args=[t.cl_a.pk]),
            {"description": "new item"},
            CARE,
        ),
        (
            "item_edit_get",
            "get",
            reverse("checklists:item_edit", args=[t.item_a.pk]),
            None,
            CARE,
        ),
        (
            "item_edit_post",
            "post",
            reverse("checklists:item_edit", args=[t.item_a.pk]),
            {"description": "edited"},
            CARE,
        ),
        (
            "item_delete",
            "post",
            reverse("checklists:item_delete", args=[t.item_a.pk]),
            None,
            CARE,
        ),
        (
            "item_toggle",
            "post",
            reverse("checklists:item_toggle", args=[t.item_a.pk]),
            None,
            FAMILY,
        ),
    ]
    return rows


# Roles whose request is refused with a redirect to login or an error are "deny".
# "root" and "timezones" are public: anonymous gets a redirect to login for "/" which is
# not an access-control failure, so they are listed as EVERYONE and judged "not error".
PUBLIC_SLUGS = {"root", "timezones"}


class AuthorizationMatrix(AuthzBase):
    """64 routes x 10 roles. See module docstring for the roles."""

    def _check(self, slug):
        rows = {row[0]: row for row in _routes(self)}
        _, method, url, data, allowed = rows[slug]
        problems = []
        for role in ROLES:
            response, changed = self.isolated(role, method, url, data)
            got = outcome(response)
            if slug in PUBLIC_SLUGS:
                if got == "error":
                    problems.append(f"{role}: 5xx")
                continue
            if role in allowed:
                if got in ("deny", "error"):
                    problems.append(
                        f"{role}: should succeed, got {response.status_code}"
                    )
            else:
                if got in ("ok", "bad", "error"):
                    problems.append(
                        f"{role}: should be refused, got {response.status_code} "
                        f"(db changed={changed})"
                    )
                if changed:
                    problems.append(
                        f"{role}: refused request still changed the database"
                    )
        self.assertEqual(problems, [], f"{slug} {method.upper()} {url}")


def _make(slug):
    def test(self):
        self._check(slug)

    test.__name__ = f"test_matrix_{slug}"
    test.__doc__ = f"Authorization matrix row: {slug}."
    return test


for _slug in [
    "root",
    "event_list",
    "choose_workspace",
    "event_list_ws",
    "event_list_ws_day",
    "create_event_form",
    "create_event_save",
    "create_event_ai",
    "scan_ocr",
    "member_colors",
    "timezones",
    "place_suggestions",
    "place_details",
    "event_detail",
    "event_detail_drivers",
    "event_edit_get",
    "event_edit_post",
    "event_delete_get",
    "event_delete_post",
    "event_transport",
    "event_trip_get",
    "event_trip_json",
    "event_trip_post",
    "event_time",
    "chat_home",
    "chat_workspace",
    "chat_messages",
    "create_workspace_get",
    "workspace_detail",
    "manage_members_get",
    "manage_members_invite",
    "resend_invite",
    "remove_member",
    "dashboard",
    "day_seen",
    "profile",
    "google_connect_login",
    "set_palette",
    "family_home_get",
    "family_home_post",
    "family_home_clear",
    "set_my_palette",
    "two_step_setup",
    "two_step_disable",
    "two_step_codes",
    "cal_connect",
    "cal_sync",
    "cal_auto_sync",
    "cal_disconnect",
    "checklist_list",
    "checklist_create_get",
    "checklist_create_post",
    "checklist_detail",
    "checklist_edit_get",
    "checklist_edit_post",
    "checklist_delete",
    "item_add",
    "item_edit_get",
    "item_edit_post",
    "item_delete",
    "item_toggle",
]:
    setattr(AuthorizationMatrix, f"test_matrix_{_slug}", _make(_slug))


# --------------------------------------------------------------------------------------
# Routes that need a custom judgement (public, token, session-bound).
# --------------------------------------------------------------------------------------


class PublicAndTokenRoutes(AuthzBase):
    def test_matrix_public_routes_do_not_error_for_any_role(self):
        token = make_invite_token(self.pending_membership)
        urls = [
            ("get", reverse("accounts:signup")),
            ("get", reverse("accounts:login")),
            ("get", reverse("accounts:two_step_verify")),
            ("get", reverse("accounts:password_reset")),
            ("get", reverse("accounts:password_reset_done")),
            ("get", reverse("accounts:password_reset_complete")),
            ("get", reverse("accounts:google_login_callback")),
            ("post", reverse("accounts:google_login")),
            ("get", reverse("accounts:accept_invite", args=[token])),
            ("get", reverse("accounts:logout")),
            ("get", reverse("calendars:google_callback")),
            ("get", reverse("events:create")),
        ]
        for role in ROLES:
            for method, url in urls:
                with self.subTest(role=role, url=url):
                    response, changed = self.isolated(role, method, url)
                    self.assertLess(response.status_code, 500)
                    self.assertFalse(changed)

    def test_control_invite_token_is_bound_to_one_membership(self):
        """A token for a pending membership cannot be replayed against another one."""
        token = make_invite_token(self.pending_membership)
        # Tamper with the payload: the signature must fail.
        tampered = token.replace(
            token.split(":")[0], token.split(":")[0][:-2] + "AA", 1
        )
        response = Client().get(reverse("accounts:accept_invite", args=[tampered]))
        self.assertEqual(response.status_code, 400)
        # Token for a confirmed member is a no-op redirect to login
        confirmed = make_invite_token(self.kid_membership)
        response = Client().post(
            reverse("accounts:accept_invite", args=[confirmed]),
            {
                "new_password1": "Another-Pass-2026!",
                "new_password2": "Another-Pass-2026!",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.users["kid"].refresh_from_db()
        self.assertTrue(self.users["kid"].check_password(PASSWORD))

    def test_control_anonymous_post_to_every_login_required_route_is_refused(self):
        anon = Client()
        for slug, method, url, data, allowed in _routes(self):
            if "anonymous" in allowed or slug in PUBLIC_SLUGS:
                continue
            with self.subTest(slug=slug):
                response = getattr(anon, method)(url, data or {})
                self.assertEqual(outcome(response), "deny", response.status_code)


# --------------------------------------------------------------------------------------
# Object lookups by sequential ids, role-only endpoints, mass assignment, foreign keys
# --------------------------------------------------------------------------------------


class ObjectLevelAccess(AuthzBase):
    def test_control_event_ids_in_other_household_are_404_on_every_event_route(self):
        """BOLA: household B credentials against household A's sequential event ids."""
        for role in ("other_parent", "other_admin", "no_household", "google_only"):
            for name, method, data in (
                ("events:detail", "get", None),
                ("events:detail", "post", {"requires_transport": "on"}),
                ("events:edit", "get", None),
                ("events:edit", "post", {"title": "pwn"}),
                ("events:delete", "get", None),
                ("events:delete", "post", {"action": "delete"}),
                ("events:set_transport_mode", "post", {"transport_mode": "walking"}),
                ("events:trip", "get", None),
                ("events:trip", "post", {"mode": "home"}),
                ("events:change_time", "post", {"starts_date": "2031-01-01"}),
            ):
                for ev in (self.ev_family, self.ev_parents, self.ev_google):
                    with self.subTest(
                        role=role, route=name, method=method, event=ev.pk
                    ):
                        response, changed = self.isolated(
                            role, method, reverse(name, args=[ev.pk]), data
                        )
                        self.assertEqual(response.status_code, 404)
                        self.assertFalse(changed)

    def test_control_kid_cannot_open_parent_only_or_google_events(self):
        for ev in (self.ev_parents, self.ev_google):
            for name in (
                "events:detail",
                "events:trip",
                "events:edit",
                "events:delete",
            ):
                with self.subTest(event=ev.title, route=name):
                    response = self.login("kid").get(reverse(name, args=[ev.pk]))
                    self.assertEqual(response.status_code, 404)
        response = self.login("kid").get(
            reverse("events:detail", args=[self.ev_kid.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_control_kid_never_sees_parent_only_titles_on_any_page(self):
        client = self.login("kid")
        pages = [
            reverse("accounts:dashboard"),
            reverse("events:list"),
            reverse("events:workspace_list", args=[self.ws_a.pk]),
            reverse("events:workspace_list", args=[self.ws_a.pk]) + "?view=day",
            reverse("events:workspace_list", args=[self.ws_a.pk]) + "?view=week",
            reverse("chat:workspace_chat", args=[self.ws_a.pk]),
            reverse("checklists:list", args=[self.ws_a.pk]),
            reverse("events:detail", args=[self.ev_family.pk]),
        ]
        for page in pages:
            body = client.get(page).content.decode()
            for secret in ("A parents only", "A parent private work meeting"):
                with self.subTest(page=page, secret=secret):
                    self.assertNotIn(secret, body)

    def test_control_checklist_ids_across_households(self):
        """Checklist and item ids of household A are 404 for household B on every route."""
        for role in ("other_parent", "other_admin", "no_household"):
            for name, method, key in (
                ("checklists:detail", "get", self.cl_a.pk),
                ("checklists:edit", "get", self.cl_a.pk),
                ("checklists:edit", "post", self.cl_a.pk),
                ("checklists:delete", "post", self.cl_a.pk),
                ("checklists:item_add", "post", self.cl_a.pk),
                ("checklists:item_edit", "get", self.item_a.pk),
                ("checklists:item_edit", "post", self.item_a.pk),
                ("checklists:item_delete", "post", self.item_a.pk),
                ("checklists:item_toggle", "post", self.item_a.pk),
            ):
                with self.subTest(role=role, route=name, method=method):
                    response, changed = self.isolated(
                        role,
                        method,
                        reverse(name, args=[key]),
                        {"title": "x", "description": "x"},
                    )
                    self.assertEqual(response.status_code, 404)
                    self.assertFalse(changed)

    def test_control_membership_ids_across_households(self):
        """Admin B cannot remove or resend household A members by guessing membership ids."""
        for name in ("accounts:remove_member", "accounts:resend_invite"):
            for membership in (self.kid_membership, self.pending_membership):
                with self.subTest(route=name, membership=membership.pk):
                    # 1: B admin on B's own workspace URL but A's membership id
                    response, changed = self.isolated(
                        "other_admin",
                        "post",
                        reverse(name, args=[self.ws_b.pk, membership.pk]),
                    )
                    self.assertFalse(changed)
                    # 2: B admin on A's workspace URL
                    response, changed = self.isolated(
                        "other_admin",
                        "post",
                        reverse(name, args=[self.ws_a.pk, membership.pk]),
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertFalse(changed)

    def test_control_parent_cannot_manage_members_or_connect_google_calendar(self):
        for name, args in (
            ("accounts:manage_members", [self.ws_a.pk]),
            ("accounts:workspace_detail", [self.ws_a.pk]),
        ):
            response = self.login("parent").get(reverse(name, args=args))
            self.assertEqual(response.status_code, 403)
        response = self.login("parent").post(
            reverse("calendars:google_connect", args=[self.ws_a.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.login("parent")
            .post(
                reverse(
                    "accounts:remove_member",
                    args=[self.ws_a.pk, self.kid_membership.pk],
                )
            )
            .status_code,
            403,
        )
        self.assertTrue(Membership.objects.filter(pk=self.kid_membership.pk).exists())

    def test_control_member_colors_foreign_user_id_is_refused(self):
        """A parent of A cannot colour a member of B, even by posting that user's id."""
        response = self.login("parent").post(
            reverse("events:member_colors", args=[self.ws_a.pk]),
            {f"color-{self.users['other_kid'].pk}": "#ff0000"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            MemberColor.objects.filter(user=self.users["other_kid"]).exists()
        )
        # and on B's workspace id
        response = self.login("parent").post(
            reverse("events:member_colors", args=[self.ws_b.pk]),
            {f"color-{self.users['other_kid'].pk}": "#ff0000"},
        )
        self.assertEqual(response.status_code, 404)

    def test_control_trip_start_row_is_per_user(self):
        """A trip-start choice made by the kid does not change the parent's start."""
        self.login("kid").post(
            reverse("events:trip", args=[self.ev_family.pk]),
            {"mode": "here", "latitude": "40.1", "longitude": "-73.1", "label": "x"},
        )
        self.assertFalse(
            TripStart.objects.filter(
                user=self.users["parent"], event=self.ev_family
            ).exists()
        )


class MassAssignmentAndForeignKeys(AuthzBase):
    def test_control_checklist_event_from_other_household_is_rejected(self):
        """checklist.event pointing at household B's event must be a form error."""
        for url, instance in (
            (reverse("checklists:create", args=[self.ws_a.pk]), None),
            (reverse("checklists:edit", args=[self.cl_a.pk]), self.cl_a),
        ):
            for ev in (self.ev_b, self.ev_b_parents):
                with self.subTest(url=url, event=ev.title):
                    response, changed = self.isolated(
                        "admin", "post", url, {"title": "linked", "event": ev.pk}
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(changed)
                    self.assertFalse(
                        Checklist.objects.filter(workspace=self.ws_a, event=ev).exists()
                    )

    def test_control_checklist_event_hidden_from_kid_not_linkable_by_kid_view(self):
        """A parent cannot link a list to another parent's private Google import."""
        response, changed = self.isolated(
            "admin",
            "post",
            reverse("checklists:create", args=[self.ws_a.pk]),
            {"title": "linked", "event": self.ev_google.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(changed)

    def test_control_checklist_mass_assignment_workspace_and_creator(self):
        response = self.login("admin").post(
            reverse("checklists:create", args=[self.ws_a.pk]),
            {
                "title": "mass",
                "workspace": self.ws_b.pk,
                "created_by": self.users["other_admin"].pk,
                "id": "00000000-0000-0000-0000-000000000001",
            },
        )
        self.assertEqual(response.status_code, 302)
        made = Checklist.objects.get(title="mass")
        self.assertEqual(made.workspace_id, self.ws_a.pk)
        self.assertEqual(made.created_by_id, self.users["admin"].pk)
        # edit: workspace move
        self.login("admin").post(
            reverse("checklists:edit", args=[self.cl_a.pk]),
            {"title": "A checklist", "workspace": self.ws_b.pk},
        )
        self.cl_a.refresh_from_db()
        self.assertEqual(self.cl_a.workspace_id, self.ws_a.pk)

    def test_control_item_mass_assignment_checklist_and_completion(self):
        self.login("admin").post(
            reverse("checklists:item_edit", args=[self.item_a.pk]),
            {
                "description": "edited",
                "checklist": self.cl_b.pk,
                "is_completed": "on",
                "completed_by": self.users["other_admin"].pk,
            },
        )
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.checklist_id, self.cl_a.pk)
        self.assertFalse(self.item_a.is_completed)
        self.assertIsNone(self.item_a.completed_by_id)

    def test_control_event_driver_form_ignores_extra_fields(self):
        response = self.login("parent").post(
            reverse("events:detail", args=[self.ev_family.pk]),
            {
                "requires_transport": "on",
                "dropoff_driver": self.users["parent"].pk,
                "title": "pwned",
                "workspace": self.ws_b.pk,
                "created_by": self.users["other_admin"].pk,
                "source": "google",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.ev_family.refresh_from_db()
        self.assertEqual(self.ev_family.title, "A family dinner")
        self.assertEqual(self.ev_family.workspace_id, self.ws_a.pk)
        self.assertEqual(self.ev_family.created_by_id, self.users["admin"].pk)
        self.assertEqual(self.ev_family.source, Event.Source.SYNCMILY)

    def test_control_driver_from_other_household_rejected(self):
        for uid in (
            self.users["other_parent"].pk,
            self.users["other_admin"].pk,
            self.users["kid"].pk,
        ):
            response = self.login("parent").post(
                reverse("events:detail", args=[self.ev_family.pk]),
                {"requires_transport": "on", "dropoff_driver": uid},
            )
            self.assertEqual(response.status_code, 200, f"driver {uid} accepted")
            self.ev_family.refresh_from_db()
            self.assertIsNone(self.ev_family.dropoff_driver_id)

    def _event_payload(self, **extra):
        return {
            "title": "Payload",
            "date": "2031-05-05",
            "start_time": "10:00",
            "end_time": "11:00",
            "timezone": "America/New_York",
            "transport_mode": "driving",
            "action": "save",
            **extra,
        }

    def test_control_participants_from_other_household_rejected_on_create_and_edit(
        self,
    ):
        foreign = [self.users["other_kid"].pk, self.users["other_parent"].pk]
        response = self.login("admin").post(
            reverse("events:workspace_create", args=[self.ws_a.pk]),
            self._event_payload(participants=foreign),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Event.objects.filter(title="Payload").exists())
        response = self.login("admin").post(
            reverse("events:edit", args=[self.ev_family.pk]),
            self._event_payload(participants=foreign),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ev_family.participants.count(), 0)

    def test_control_create_event_mass_assignment_workspace_created_by_source(self):
        form = self.login("admin").get(
            reverse("events:workspace_create", args=[self.ws_a.pk])
        )
        token = form.context["form"].fields["submission_token"].initial
        response = self.login("admin").post(
            reverse("events:workspace_create", args=[self.ws_a.pk]),
            self._event_payload(
                submission_token=token,
                workspace=self.ws_b.pk,
                created_by=self.users["other_admin"].pk,
                source="google",
                calendar_connection=self.conn.pk,
                external_id="x",
            ),
        )
        self.assertEqual(response.status_code, 302)
        made = Event.objects.get(title="Payload")
        self.assertEqual(made.workspace_id, self.ws_a.pk)
        self.assertEqual(made.created_by_id, self.users["admin"].pk)
        self.assertEqual(made.source, Event.Source.SYNCMILY)
        self.assertIsNone(made.calendar_connection_id)

    def test_control_create_event_token_from_other_user_or_workspace_rejected(self):
        """The signed submission token is bound to user and workspace."""
        token = signing.dumps(
            {
                "id": "8d3a2c5e-1f20-4f6a-9a53-0d1f3b6f2a11",
                "user": self.users["parent"].pk,
                "workspace": str(self.ws_a.pk),
            },
            salt="events.create",
        )
        response = self.login("admin").post(
            reverse("events:workspace_create", args=[self.ws_a.pk]),
            self._event_payload(submission_token=token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Event.objects.filter(title="Payload").exists())

    def test_control_edit_event_cannot_move_workspace(self):
        self.login("admin").post(
            reverse("events:edit", args=[self.ev_family.pk]),
            self._event_payload(
                workspace=self.ws_b.pk, created_by=self.users["other_admin"].pk
            ),
        )
        self.ev_family.refresh_from_db()
        self.assertEqual(self.ev_family.workspace_id, self.ws_a.pk)
        self.assertEqual(self.ev_family.created_by_id, self.users["admin"].pk)

    def test_control_invite_form_cannot_pick_workspace_or_admin_role(self):
        """role is a Parent/Kid choice; no extra field changes workspace or admin flags."""
        response = self.login("admin").post(
            reverse("accounts:manage_members", args=[self.ws_a.pk]),
            {
                "role": "admin",
                "email": "x@example.test",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            get_user_model().objects.filter(email="x@example.test").exists()
        )
        self.login("admin").post(
            reverse("accounts:manage_members", args=[self.ws_a.pk]),
            {
                "role": "kid",
                "email": "y@example.test",
                "workspace": self.ws_b.pk,
                "is_staff": "on",
                "is_superuser": "on",
            },
        )
        invited = get_user_model().objects.get(email="y@example.test")
        self.assertFalse(invited.is_staff or invited.is_superuser)
        self.assertEqual(invited.membership.workspace_id, self.ws_a.pk)

    def test_control_profile_username_form_ignores_privilege_fields(self):
        self.login("kid").post(
            reverse("accounts:profile"),
            {
                "action": "username",
                "username": "KidRenamed",
                "is_staff": "on",
                "is_superuser": "on",
                "is_active": "on",
                "email": "evil@example.test",
            },
        )
        kid = self.users["kid"]
        kid.refresh_from_db()
        self.assertEqual(kid.username, "KidRenamed")
        self.assertFalse(kid.is_staff or kid.is_superuser)
        self.assertEqual(kid.email, "kid" "a@example.test")

    def test_control_workspace_create_ignores_primary_admin_field(self):
        response = self.login("no_household").post(
            reverse("accounts:create_workspace"),
            {
                "name": "Mine",
                "primary_admin": self.users["admin"].pk,
                "palette": "bold",
                "home_location": "x",
            },
        )
        self.assertEqual(response.status_code, 302)
        made = Workspace.objects.get(name="Mine")
        self.assertEqual(made.primary_admin_id, self.users["no_household"].pk)

    def test_control_palette_endpoints_reject_unknown_values_and_wrong_role(self):
        self.assertEqual(
            self.login("parent")
            .post(
                reverse("accounts:set_palette", args=[self.ws_a.pk]),
                {"palette": "evil"},
            )
            .status_code,
            400,
        )
        self.assertEqual(
            self.login("kid")
            .post(
                reverse("accounts:set_palette", args=[self.ws_a.pk]),
                {"palette": "bold"},
            )
            .status_code,
            404,
        )
        self.assertEqual(
            self.login("parent")
            .post(
                reverse("accounts:set_my_palette", args=[self.ws_a.pk]),
                {"palette": "bold"},
            )
            .status_code,
            404,
        )
        # kid of B cannot set palette on A through own membership
        self.assertEqual(
            self.clients["other_parent"]
            .post(
                reverse("accounts:set_my_palette", args=[self.ws_a.pk]),
                {"palette": "bold"},
            )
            .status_code,
            404,
        )

    def test_control_open_redirect_parameters_are_pinned_to_the_site(self):
        for url, role in (
            (reverse("accounts:set_palette", args=[self.ws_a.pk]), "parent"),
            (reverse("events:member_colors", args=[self.ws_a.pk]), "parent"),
            (reverse("events:set_transport_mode", args=[self.ev_family.pk]), "parent"),
        ):
            response = self.login(role).post(
                url,
                {
                    "palette": "bold",
                    "transport_mode": "walking",
                    "next": "https://evil.example/x",
                },
            )
            self.assertEqual(response.status_code, 302)
            self.assertNotIn("evil.example", response["Location"])


# --------------------------------------------------------------------------------------
# PoCs for confirmed findings
# --------------------------------------------------------------------------------------


class HomeCoordinateLeak(AuthzBase):
    def _trip_card(self, role, mode=None):
        url = reverse("events:trip", args=[self.ev_family.pk])
        if mode:
            self.login(role).post(url, {"mode": mode})
        return self.login(role).get(url).content.decode()

    def test_poc_AUTHZ_001_kid_trip_card_must_not_contain_family_home_coordinates_default(
        self,
    ):
        """AUTHZ-001: family_home.html promises 'only parents see or change the address'.
        With no saved choice the default origin falls back to Home, and maps_url() puts
        the home latitude and longitude in the 'Open in Google Maps' href.
        """
        body = self._trip_card("kid")
        self.assertIn("Open in Google Maps", body)  # the card rendered
        self.assertNotIn("40.6892", body)
        self.assertNotIn("73.9442", body)

    def test_poc_AUTHZ_001_kid_trip_card_must_not_contain_family_home_coordinates_home_mode(
        self,
    ):
        """AUTHZ-001: same leak when the kid taps the Home chip (POST mode=home)."""
        body = self._trip_card("kid", mode="home")
        self.assertNotIn("40.6892", body)
        self.assertNotIn("73.9442", body)

    def test_control_parent_trip_card_may_use_home_coordinates(self):
        """Parents may see where home is, so the same link is fine for them."""
        body = self._trip_card("parent", mode="home")
        self.assertIn("40.6892", body)

    def test_control_kid_json_marker_and_event_pages_do_not_contain_home(self):
        client = self.login("kid")
        for page in (
            reverse("events:trip", args=[self.ev_family.pk]) + "?format=json",
            reverse("events:detail", args=[self.ev_family.pk]),
            reverse("accounts:dashboard"),
            reverse("events:workspace_list", args=[self.ws_a.pk]) + "?view=day",
        ):
            body = client.get(page).content.decode()
            self.assertNotIn("40.6892", body, page)
            self.assertNotIn("Evergreen", body, page)

    def test_control_kid_cannot_read_or_change_family_home(self):
        self.assertEqual(
            self.login("kid")
            .get(reverse("accounts:family_home", args=[self.ws_a.pk]))
            .status_code,
            404,
        )
        response = self.login("kid").post(
            reverse("accounts:family_home", args=[self.ws_a.pk]),
            {"action": "save", "location": "x", "latitude": "1", "longitude": "2"},
        )
        self.assertEqual(response.status_code, 404)
        self.ws_a.refresh_from_db()
        self.assertEqual(self.ws_a.home_location, HOME_TEXT)


class PaidApiAccess(AuthzBase):
    def test_poc_AUTHZ_002_places_proxy_must_not_serve_accounts_without_a_household(
        self,
    ):
        """AUTHZ-002: /events/places/ and /events/places/details/ only need login. Open
        sign-up with no email check means any throwaway account can drive paid Google
        Places calls. They are only needed on screens a household member reaches.
        """
        for role in ("no_household", "google_only", "pending_active"):
            self.mocks["places_suggest"].reset_mock()
            self.mocks["places_details"].reset_mock()
            self.login(role).post(
                reverse("events:place_suggestions"), {"q": "Brooklyn"}
            )
            self.login(role).post(
                reverse("events:place_details"), {"id": "ChIJabcdefghijklmnop"}
            )
            with self.subTest(role=role):
                self.assertEqual(self.mocks["places_suggest"].call_count, 0)
                self.assertEqual(self.mocks["places_details"].call_count, 0)

    def test_control_ocr_and_ai_calls_are_caregiver_only(self):
        for role in (
            "kid",
            "other_parent",
            "other_admin",
            "no_household",
            "google_only",
        ):
            self.login(role).post(
                reverse("events:scan_ocr", args=[self.ws_a.pk]), {"timezone": "UTC"}
            )
            self.login(role).post(
                reverse("events:workspace_create", args=[self.ws_a.pk]),
                {"action": "generate_suggestion", "description": "x"},
            )
            self.login(role).post(
                reverse("events:create"),
                {"action": "generate_suggestion", "description": "x"},
            )
        self.assertEqual(self.mocks["scan"].call_count, 0)
        self.assertEqual(self.mocks["suggestion"].call_count, 0)
        # anonymous POST to /events/new/ is a login redirect and calls nothing
        response = Client().post(
            reverse("events:create"),
            {"action": "generate_suggestion", "description": "x"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.mocks["suggestion"].call_count, 0)

    def test_control_google_calendar_connect_is_primary_admin_only(self):
        for role in (
            "kid",
            "parent",
            "other_parent",
            "other_admin",
            "no_household",
            "google_only",
        ):
            response = self.login(role).post(
                reverse("calendars:google_connect", args=[self.ws_a.pk])
            )
            self.assertEqual(response.status_code, 404, role)
        self.assertEqual(self.mocks["g_auth"].call_count, 0)


class PendingInviteeState(AuthzBase):
    def test_poc_AUTHZ_003_unconfirmed_membership_must_not_open_the_household_dashboard(
        self,
    ):
        """AUTHZ-003 (defense in depth): get_family_context() and the dashboard ignore
        Membership.accepted_at. Every other gate (family_workspaces, chat access) requires
        it. An ACTIVE account with a pending membership is not reachable through the
        normal invite flow today, but the dashboard would show the household's member
        list the day any path (admin edit, bug, future Google invite flow) activates one.
        """
        response = self.login("pending_active").get(reverse("accounts:dashboard"))
        body = response.content.decode()
        self.assertNotIn("ParentA", body)
        self.assertNotIn("AdminA", body)
        self.assertNotIn("KidA", body)
        self.assertNotIn("Household A", body)

    def test_control_pending_invitee_is_refused_on_tenant_routes(self):
        for role in ("pending_inactive", "pending_active"):
            for url in (
                reverse("events:workspace_list", args=[self.ws_a.pk]),
                reverse("events:detail", args=[self.ev_family.pk]),
                reverse("checklists:list", args=[self.ws_a.pk]),
                reverse("chat:workspace_chat", args=[self.ws_a.pk]),
                reverse("chat:messages", args=[self.ws_a.pk]),
            ):
                with self.subTest(role=role, url=url):
                    self.assertIn(outcome(self.login(role).get(url)), ("deny",))


class KidPrivacyAndGoogleLink(AuthzBase):
    def test_control_kid_pages_do_not_show_other_members_emails(self):
        client = self.login("kid")
        for page in (
            reverse("accounts:dashboard"),
            reverse("events:workspace_list", args=[self.ws_a.pk]) + "?view=day",
            reverse("chat:workspace_chat", args=[self.ws_a.pk]),
            reverse("checklists:list", args=[self.ws_a.pk]),
            reverse("accounts:profile"),
        ):
            body = client.get(page).content.decode().lower()
            for email in (
                "admina@example.test",
                "parenta@example.test",
                "pendinginactive@example.test",
            ):
                with self.subTest(page=page, email=email):
                    self.assertNotIn(email, body)

    def test_control_google_only_user_cannot_drop_their_only_sign_in(self):
        self.login("google_only").post(reverse("accounts:google_disconnect"))
        self.assertTrue(
            GoogleIdentity.objects.filter(user=self.users["google_only"]).exists()
        )
