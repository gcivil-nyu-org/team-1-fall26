import secrets
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Membership, Workspace
from events.models import Event

WORKSPACE_NAME = "Demo family"
PARENT, OTHER_PARENT, KID = "demo-parent", "demo-parent-2", "demo-kid"


class Command(BaseCommand):
    help = (
        "Local development only: (re)create a demo family with a kid whose day has"
        ' plans with and without drivers, so the kid view and its "Your day" pop-up can'
        " be checked by signing in as the kid."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            help="Password for the three demo accounts (default: a new random one).",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed_demo_family only runs with DJANGO_DEBUG=true; it never touches"
                " production."
            )
        password = options["password"] or secrets.token_urlsafe(12)
        User = get_user_model()
        with transaction.atomic():
            old = Workspace.objects.filter(
                name=WORKSPACE_NAME, primary_admin__username=PARENT
            )
            Event.objects.filter(workspace__in=old).delete()
            old.delete()
            User.objects.filter(username__in=[PARENT, OTHER_PARENT, KID]).delete()

            parent = User.objects.create_user(
                username=PARENT, first_name="Ana", password=password
            )
            other = User.objects.create_user(
                username=OTHER_PARENT, first_name="Mateo", password=password
            )
            kid = User.objects.create_user(
                username=KID, first_name="Lucia", password=password
            )
            workspace = Workspace.objects.create(
                name=WORKSPACE_NAME, primary_admin=parent
            )
            now = timezone.now()
            for user, role in (
                (parent, Membership.Role.PARENT),
                (other, Membership.Role.PARENT),
                (kid, Membership.Role.KID),
            ):
                Membership.objects.create(
                    workspace=workspace,
                    user=user,
                    role=role,
                    invited_by=parent,
                    accepted_at=now,
                )

            tz = timezone.get_current_timezone()
            today = timezone.localdate()

            def at(day_offset, hour, minute=0):
                return datetime.combine(
                    today + timedelta(days=day_offset), time(hour, minute), tzinfo=tz
                )

            plans = [
                # title, start, end, people, transport (drop-off, pickup, mode),
                # tentative
                (
                    "School",
                    at(0, 8),
                    at(0, 15),
                    [kid],
                    (parent, None, Event.TransportMode.DRIVING),
                    False,
                    "PS 41",
                ),
                (
                    "Soccer practice",
                    at(0, 16, 30),
                    at(0, 18),
                    [kid],
                    (other, parent, Event.TransportMode.DRIVING),
                    False,
                    "Pier 40",
                ),
                # Overlaps soccer, so the parents' briefing has a double-booking to
                # flag.
                (
                    "Dentist",
                    at(0, 17),
                    at(0, 17, 45),
                    [kid],
                    (parent, parent, Event.TransportMode.DRIVING),
                    False,
                    "Smile Dental",
                ),
                ("Family dinner", at(0, 19), at(0, 20), [], None, False, ""),
                (
                    "Piano lesson",
                    at(0, 21),
                    at(0, 21, 45),
                    [kid],
                    (None, None, Event.TransportMode.WALKING),
                    False,
                    "",
                ),
                (
                    "Field trip",
                    at(1, 0),
                    at(2, 0),
                    [kid],
                    None,
                    False,
                    "Museum of Natural History",
                ),
                (
                    "Sleepover at Maya's",
                    at(1, 18),
                    at(2, 10),
                    [kid],
                    (parent, None, Event.TransportMode.TRANSIT),
                    True,
                    "",
                ),
                (
                    "Parents' night out",
                    at(0, 20),
                    at(0, 23),
                    [parent, other],
                    None,
                    False,
                    "",
                ),
            ]
            for title, starts_at, ends_at, people, ride, tentative, location in plans:
                dropoff, pickup, mode = ride or (
                    None,
                    None,
                    Event.TransportMode.TRANSIT,
                )
                event = Event.objects.create(
                    created_by=parent,
                    workspace=workspace,
                    title=title,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    location=location,
                    requires_transport=ride is not None,
                    dropoff_driver=dropoff,
                    pickup_driver=pickup,
                    transport_mode=mode,
                    is_tentative=tentative,
                )
                event.participants.set(people)

        self.stdout.write(
            self.style.SUCCESS(
                f"Demo family ready. Sign in as {KID} (the kid) or {PARENT} (a parent)"
                f" with password: {password}"
            )
        )
