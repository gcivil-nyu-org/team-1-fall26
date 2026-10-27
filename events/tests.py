from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.db.models.deletion import ProtectedError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace
from accounts.services import invite_member, make_invite_token

from .forms import EventForm, EventScheduleForm
from .models import Event


class EventFormTests(SimpleTestCase):
    def setUp(self):
        self.event_data = {
            "title": "Soccer practice",
            "date": "2026-10-20",
            "start_time": "15:30",
            "end_time": "16:30",
            "location": "School field",
            "transport_mode": "transit",
        }

    def test_valid_event(self):
        form = EventForm(data=self.event_data)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["date"], date(2026, 10, 20))
        self.assertEqual(form.cleaned_data["start_time"], time(15, 30))
        self.assertEqual(form.cleaned_data["end_time"], time(16, 30))

    def test_location_is_optional(self):
        self.event_data.pop("location")
        form = EventForm(data=self.event_data)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["location"], "")

    def test_missing_required_fields(self):
        for field in ("title", "date", "start_time", "end_time"):
            with self.subTest(field=field):
                data = self.event_data.copy()
                data.pop(field)
                form = EventForm(data=data)

                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors.as_data()[field][0].code, "required")

    def test_blank_title_is_rejected(self):
        self.event_data["title"] = "   "
        form = EventForm(data=self.event_data)

        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_end_time_must_be_after_start_time(self):
        for end_time in ("15:30", "14:30"):
            with self.subTest(end_time=end_time):
                data = {**self.event_data, "end_time": end_time}
                form = EventForm(data=data)

                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors["end_time"], ["End time must be after start time."]
                )

    def test_invalid_dates_and_times(self):
        for field, value in (
            ("date", "2026-02-30"),
            ("start_time", "25:00"),
            ("end_time", "not a time"),
        ):
            with self.subTest(field=field):
                data = {**self.event_data, field: value}
                form = EventForm(data=data)

                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors.as_data()[field][0].code, "invalid")


class WorkspaceEventTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(username="family_admin")
        self.other_user = get_user_model().objects.create_user(username="other_admin")
        self.workspace = Workspace.objects.create(name="Our family", primary_admin=self.admin)
        self.other_workspace = Workspace.objects.create(name="Other family", primary_admin=self.other_user)
        self.create_url = reverse("events:workspace_create", args=[self.workspace.pk])
        self.list_url = reverse("events:workspace_list", args=[self.workspace.pk])
        self.event_data = {
            "title": "Family dinner",
            "date": "2026-10-20",
            "start_time": "18:00",
            "end_time": "19:00",
            "action": "save",
            "transport_mode": "transit",
        }
        self.client.force_login(self.admin)

    def submission_token(self, url):
        return self.client.get(url).context["form"]["submission_token"].value()

    def create_saved_event(self, title, workspace, creator):
        return Event.objects.create(
            title=title,
            workspace=workspace,
            created_by=creator,
            starts_at=datetime(2026, 10, 20, 18, tzinfo=timezone.utc),
            ends_at=datetime(2026, 10, 20, 19, tzinfo=timezone.utc),
        )

    def test_primary_admin_can_save_without_global_event_permission(self):
        token = self.submission_token(self.create_url)
        response = self.client.post(self.create_url, {**self.event_data, "submission_token": token})
        event = Event.objects.get()
        self.assertRedirects(response, reverse("events:detail", args=[event.pk]) + "?timezone=America%2FNew_York")
        self.assertEqual(event.workspace, self.workspace)
        self.assertEqual(event.created_by, self.admin)
        retry = self.client.post(self.create_url, {**self.event_data, "submission_token": token})
        self.assertRedirects(retry, response.url)
        self.assertEqual(Event.objects.count(), 1)

    def test_other_user_cannot_create_or_list_workspace_events(self):
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(self.create_url).status_code, 404)
        self.assertEqual(self.client.post(self.create_url, self.event_data).status_code, 404)
        self.assertEqual(self.client.get(self.list_url).status_code, 404)
        self.assertFalse(Event.objects.exists())

    def test_anonymous_workspace_requests_require_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.create_url).status_code, 302)
        self.assertEqual(self.client.post(self.create_url, self.event_data).status_code, 302)
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertFalse(Event.objects.exists())

    def test_submission_cannot_be_reused_in_another_workspace(self):
        second_workspace = Workspace.objects.create(name="Second family", primary_admin=self.admin)
        other_url = reverse("events:workspace_create", args=[second_workspace.pk])
        token = self.submission_token(self.create_url)
        response = self.client.post(other_url, {**self.event_data, "submission_token": token})
        self.assertContains(response, "This form has expired or is invalid.")
        self.assertFalse(Event.objects.exists())

    def test_schedule_is_scoped_to_workspace_and_keeps_navigation(self):
        own_event = self.create_saved_event("Our dinner", self.workspace, self.admin)
        self.create_saved_event("Other dinner", self.other_workspace, self.other_user)
        self.create_saved_event("Earlier personal event", None, self.admin)
        response = self.client.get(self.list_url, {"date": "2026-10-20", "view": "week"})
        self.assertEqual(list(response.context["page"]), [own_event])
        self.assertContains(response, self.create_url)
        self.assertContains(response, "view=week")

    def test_workspace_access_overrides_creator_for_linked_events(self):
        event = self.create_saved_event("Shared event", self.workspace, self.other_user)
        detail_url = reverse("events:detail", args=[event.pk])
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        response = self.client.get(reverse("events:list"), {"date": "2026-10-20"})
        self.assertNotContains(response, event.title)

    def test_earlier_personal_events_remain_private(self):
        event = self.create_saved_event("Earlier event", None, self.admin)
        detail_url = reverse("events:detail", args=[event.pk])
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(detail_url).status_code, 404)

    def test_workspace_page_links_to_events_and_generic_form_cannot_save(self):
        response = self.client.get(reverse("accounts:workspace_detail", args=[self.workspace.pk]))
        self.assertContains(response, self.create_url)
        self.assertContains(response, self.list_url)
        response = self.client.post(reverse("events:create"), self.event_data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Event.objects.exists())

    def test_workspace_with_events_cannot_be_deleted(self):
        self.create_saved_event("Family dinner", self.workspace, self.admin)
        with self.assertRaises(ProtectedError):
            self.workspace.delete()


    def test_create_form_uses_schedule_date_and_timezone(self):
        response = self.client.get(self.create_url, {
            "date": "2026-11-01", "timezone": "America/New_York",
        })
        self.assertEqual(response.context["form"]["date"].value(), date(2026, 11, 1))
        self.assertEqual(response.context["form"]["timezone"].value(), "America/New_York")

    def test_saved_event_keeps_local_date_in_schedule_link(self):
        token = self.submission_token(self.create_url)
        response = self.client.post(self.create_url, {
            **self.event_data,
            "start_time": "23:00",
            "end_time": "23:30",
            "timezone": "America/New_York",
            "submission_token": token,
        }, follow=True)
        self.assertContains(response, "11:00 PM")
        self.assertEqual(response.context["timezone_name"], "America/New_York")
        self.assertIn("date=2026-10-20", response.context["schedule_query"])
        self.assertIn("timezone=America%2FNew_York", response.context["schedule_query"])
        schedule = self.client.get(self.list_url, {
            "date": "2026-10-20", "timezone": "America/New_York",
        })
        self.assertEqual(list(schedule.context["page"]), [Event.objects.get()])
        self.assertContains(schedule, "11:00 PM")
        for key in ("previous_query", "next_query", "period_query", "create_query"):
            self.assertIn("timezone=America%2FNew_York", schedule.context[key])

    def test_schedule_defaults_to_new_york_time(self):
        self.create_saved_event("Family dinner", self.workspace, self.admin)
        response = self.client.get(self.list_url, {"date": "2026-10-20"})
        self.assertEqual(response.context["timezone_name"], "America/New_York")
        self.assertContains(response, "2:00 PM")

    def test_new_york_times_hold_across_the_clock_change(self):
        days = ["2026-10-31", "2026-11-02"]
        for day in days:
            token = self.submission_token(self.create_url)
            self.client.post(self.create_url, {
                **self.event_data, "date": day, "start_time": "10:00", "end_time": "11:00",
                "submission_token": token,
            })
        events = list(Event.objects.order_by("starts_at"))
        self.assertEqual([event.starts_at.hour for event in events], [14, 15])
        self.assertEqual({event.timezone_name for event in events}, {"America/New_York"})
        for day in days:
            self.assertContains(self.client.get(self.list_url, {"date": day}), "10:00 AM")

    def test_invalid_schedule_timezone_shows_error(self):
        response = self.client.get(self.list_url, {"timezone": "invalid"})
        self.assertIn("timezone", response.context["form"].errors)
        self.assertEqual(list(response.context["page"]), [])

    def test_invalid_detail_timezone_shows_error_without_crashing(self):
        event = self.create_saved_event("Family dinner", self.workspace, self.admin)
        response = self.client.get(reverse("events:detail", args=[event.pk]), {"timezone": "invalid"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("timezone", response.context["timezone_form"].errors)


    def test_main_create_page_opens_workspace_picker(self):
        response = self.client.get(reverse("events:create"), {
            "date": "2026-10-20", "timezone": "America/New_York",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("events:choose_workspace")))
        self.assertIn("date=2026-10-20", response.url)

    def test_picker_only_offers_managed_workspaces(self):
        response = self.client.get(reverse("events:choose_workspace"))
        self.assertEqual(list(response.context["form"].fields["workspace"].queryset), [self.workspace])
        response = self.client.get(reverse("events:choose_workspace"), {
            "workspace": self.other_workspace.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("workspace", response.context["form"].errors)

    def test_picker_preserves_date_and_timezone(self):
        response = self.client.get(reverse("events:choose_workspace"), {
            "workspace": self.workspace.pk,
            "date": "2026-10-20",
            "timezone": "America/New_York",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(self.create_url))
        self.assertIn("date=2026-10-20", response.url)
        self.assertIn("timezone=America%2FNew_York", response.url)

    def test_picker_handles_no_workspaces_and_requires_login(self):
        user = get_user_model().objects.create_user(username="no_workspace")
        self.client.force_login(user)
        response = self.client.get(reverse("events:choose_workspace"))
        self.assertContains(response, "You need to be a primary admin or an accepted parent")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("events:choose_workspace")).status_code, 302)

    def test_upcoming_schedule_includes_ongoing_and_future_only(self):
        now = datetime(2026, 10, 20, 18, 30, tzinfo=timezone.utc)
        ongoing = self.create_saved_event("Ongoing dinner", self.workspace, self.admin)
        ended = self.create_saved_event("Finished dinner", self.workspace, self.admin)
        Event.objects.filter(pk=ended.pk).update(ends_at=now)
        future = self.create_saved_event("Next month dinner", self.workspace, self.admin)
        Event.objects.filter(pk=future.pk).update(
            starts_at=now + timedelta(days=30), ends_at=now + timedelta(days=30, hours=1),
        )
        self.create_saved_event("Private other family", self.other_workspace, self.other_user)
        with patch("events.forms.timezone.now", return_value=now):
            response = self.client.get(self.list_url, {"view": "upcoming"})
        self.assertEqual(list(response.context["page"]), [ongoing, future])
        self.assertContains(response, "Upcoming and ongoing events")
        self.assertNotContains(response, "Previous upcoming")

    def test_upcoming_pagination_preserves_timezone_and_workspace(self):
        for number in range(21):
            self.create_saved_event(f"Family activity {number}", self.workspace, self.admin)
        now = datetime(2026, 10, 20, 17, tzinfo=timezone.utc)
        with patch("events.forms.timezone.now", return_value=now):
            response = self.client.get(self.list_url, {
                "view": "upcoming", "page": "2", "timezone": "America/New_York",
            })
        self.assertEqual(len(response.context["page"]), 1)
        self.assertIn("view=upcoming", response.context["period_query"])
        self.assertIn("timezone=America%2FNew_York", response.context["period_query"])

    def test_combined_upcoming_schedule_identifies_workspace(self):
        self.create_saved_event("Family activity", self.workspace, self.admin)
        now = datetime(2026, 10, 20, 17, tzinfo=timezone.utc)
        with patch("events.forms.timezone.now", return_value=now):
            response = self.client.get(reverse("events:list"), {"view": "upcoming"})
        self.assertContains(response, self.workspace.name)
        self.assertContains(response, reverse("events:choose_workspace"))


    def test_reopened_event_uses_original_timezone_without_query_parameters(self):
        token = self.submission_token(self.create_url)
        self.client.post(self.create_url, {
            **self.event_data,
            "timezone": "America/New_York",
            "start_time": "23:00",
            "end_time": "23:30",
            "submission_token": token,
        })
        event = Event.objects.get()
        self.assertEqual(event.timezone_name, "America/New_York")
        self.assertEqual(event.starts_at.hour, 3)
        response = self.client.get(reverse("events:detail", args=[event.pk]))
        self.assertEqual(response.context["timezone_name"], "America/New_York")
        self.assertContains(response, "11:00 PM")
        self.assertIn("date=2026-10-20", response.context["schedule_query"])

    def test_display_timezone_override_does_not_change_saved_timezone(self):
        event = self.create_saved_event("Family dinner", self.workspace, self.admin)
        event.timezone_name = "America/New_York"
        event.save(update_fields=["timezone_name"])
        response = self.client.get(reverse("events:detail", args=[event.pk]), {"timezone": "Asia/Kolkata"})
        self.assertEqual(response.context["timezone_name"], "Asia/Kolkata")
        event.refresh_from_db()
        self.assertEqual(event.timezone_name, "America/New_York")

    def test_failed_save_keeps_details_and_allows_safe_retry(self):
        token = self.submission_token(self.create_url)
        data = {
            **self.event_data,
            "timezone": "America/New_York",
            "location": "School field",
            "submission_token": token,
        }
        with patch("events.views.Event.objects.get_or_create", side_effect=OperationalError("Temporary failure")):
            response = self.client.post(self.create_url, data)
        self.assertContains(response, "Your details are still here.")
        for field in ("title", "date", "start_time", "end_time", "timezone", "location", "submission_token"):
            self.assertEqual(response.context["form"][field].value(), data[field])
        self.assertFalse(Event.objects.exists())
        response = self.client.post(self.create_url, data)
        self.assertEqual(response.status_code, 302)
        self.client.post(self.create_url, data)
        self.assertEqual(Event.objects.count(), 1)

    def test_invalid_event_does_not_save_and_keeps_entered_values(self):
        token = self.submission_token(self.create_url)
        response = self.client.post(self.create_url, {
            **self.event_data,
            "end_time": "17:00",
            "submission_token": token,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("end_time", response.context["form"].errors)
        self.assertEqual(response.context["form"]["title"].value(), self.event_data["title"])
        self.assertFalse(Event.objects.exists())


class EventScheduleTimezoneTests(SimpleTestCase):
    def test_daylight_saving_day_uses_local_midnights(self):
        form = EventScheduleForm({"date": "2026-11-01", "timezone": "America/New_York"})
        self.assertTrue(form.is_valid(), form.errors)
        starts_at = form.cleaned_data["starts_at"].astimezone(timezone.utc)
        ends_at = form.cleaned_data["ends_at"].astimezone(timezone.utc)
        self.assertEqual(ends_at - starts_at, timedelta(hours=25))


class EventModelTimezoneTests(SimpleTestCase):
    def test_invalid_timezone_is_rejected(self):
        event = Event(title="Family dinner", timezone_name="Not/A_Timezone")
        with self.assertRaises(ValidationError) as error:
            event.clean()
        self.assertIn("timezone_name", error.exception.message_dict)


class FamilyEventTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="PrimaryParent")
        cls.parent = User.objects.create_user(username="AcceptedParent", password="FamilyTest2026!")
        cls.kid = User.objects.create_user(username="AcceptedKid")
        cls.pending = User.objects.create_user(username="PendingParent")
        cls.inactive = User.objects.create_user(username="InactiveKid", is_active=False)
        cls.outsider = User.objects.create_user(username="OtherFamilyParent")
        cls.workspace = Workspace.objects.create(name="Shared Family", primary_admin=cls.admin)
        cls.other_workspace = Workspace.objects.create(name="Other Family", primary_admin=cls.outsider)
        for user, role, accepted in (
            (cls.parent, Membership.Role.PARENT, True),
            (cls.kid, Membership.Role.KID, True),
            (cls.pending, Membership.Role.PARENT, False),
            (cls.inactive, Membership.Role.KID, True),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.admin,
                accepted_at=django_timezone.now() if accepted else None,
            )

    def setUp(self):
        self.client.force_login(self.parent)
        self.create_url = reverse("events:workspace_create", args=[self.workspace.pk])
        self.list_url = reverse("events:workspace_list", args=[self.workspace.pk])
        self.event_date = django_timezone.localdate() + timedelta(days=1)
        self.data = {
            "title": "Family picnic",
            "date": self.event_date.isoformat(),
            "start_time": "15:30",
            "end_time": "17:00",
            "timezone": "America/New_York",
            "location": "Riverside Park",
            "action": "save",
            "transport_mode": "transit",
        }

    def token(self):
        response = self.client.get(self.create_url)
        self.assertEqual(response.status_code, 200)
        return response.context["form"]["submission_token"].value()

    def save_event(self, participants=None):
        return self.client.post(self.create_url, {
            **self.data,
            "submission_token": self.token(),
            "participants": participants or [],
        })

    def test_accepted_parent_can_create_with_family_members(self):
        response = self.save_event([self.admin.pk, self.parent.pk, self.kid.pk])
        event = Event.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(event.workspace, self.workspace)
        self.assertEqual(event.created_by, self.parent)
        self.assertEqual(event.timezone_name, "America/New_York")
        self.assertEqual(event.location, self.data["location"])
        self.assertSetEqual(set(event.participants.all()), {self.admin, self.parent, self.kid})
        detail = self.client.get(response.url)
        self.assertContains(detail, self.kid.username)
        self.assertEqual(detail.context["workspace_url"], reverse("accounts:dashboard"))

    def test_participants_are_optional_and_location_is_optional(self):
        data = {**self.data, "location": "", "submission_token": self.token()}
        response = self.client.post(self.create_url, data)
        self.assertEqual(response.status_code, 302)
        event = Event.objects.get()
        self.assertEqual(event.location, "")
        self.assertFalse(event.participants.exists())

    def test_participant_choices_are_limited_to_active_accepted_family(self):
        response = self.client.get(self.create_url)
        choices = response.context["form"].fields["participants"].queryset
        self.assertSetEqual(set(choices), {self.admin, self.parent, self.kid})

    def test_preview_shows_selected_family_without_saving(self):
        response = self.client.post(self.create_url, {
            **self.data,
            "action": "preview",
            "participants": [self.kid.pk],
        })
        self.assertContains(response, "Review your event")
        self.assertContains(response, self.kid.username)
        self.assertEqual(list(response.context["form"].cleaned_data["participants"]), [self.kid])
        self.assertFalse(Event.objects.exists())

    def test_missing_required_details_preserve_participants_and_show_errors(self):
        response = self.client.post(self.create_url, {
            "action": "save", "submission_token": self.token(), "participants": [self.kid.pk],
        })
        self.assertEqual(response.status_code, 200)
        for field in ("title", "date", "start_time", "end_time"):
            self.assertIn(field, response.context["form"].errors)
        self.assertEqual(response.context["form"]["participants"].value(), [str(self.kid.pk)])
        self.assertFalse(Event.objects.exists())

    def test_forged_or_ineligible_participants_are_rejected(self):
        token = self.token()
        for participant in (self.outsider.pk, self.pending.pk, self.inactive.pk, 999999, "invalid"):
            with self.subTest(participant=participant):
                response = self.client.post(self.create_url, {
                    **self.data, "submission_token": token, "participants": [participant],
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn("participants", response.context["form"].errors)
                self.assertFalse(Event.objects.exists())

    def test_previewed_member_must_still_be_eligible_when_saved(self):
        token = self.token()
        data = {**self.data, "submission_token": token, "participants": [self.kid.pk]}
        self.assertEqual(self.client.post(self.create_url, {**data, "action": "preview"}).status_code, 200)
        Membership.objects.filter(user=self.kid).delete()
        response = self.client.post(self.create_url, data)
        self.assertIn("participants", response.context["form"].errors)
        self.assertFalse(Event.objects.exists())

    def test_primary_admin_can_select_members_without_a_membership_record(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.save_event([self.admin.pk, self.kid.pk]).status_code, 302)
        self.assertSetEqual(set(Event.objects.get().participants.all()), {self.admin, self.kid})

    def test_kid_can_read_shared_event_even_without_being_selected(self):
        self.save_event([self.parent.pk])
        event = Event.objects.get()
        self.client.force_login(self.kid)
        detail = self.client.get(reverse("events:detail", args=[event.pk]))
        self.assertContains(detail, self.data["title"])
        self.assertContains(detail, 'aria-current="page"', count=1)
        self.assertNotContains(detail, "Create another event")
        for view in ("day", "week", "upcoming"):
            response = self.client.get(self.list_url, {"view": view, "date": self.event_date.isoformat()})
            self.assertContains(response, self.data["title"])
            self.assertNotContains(response, "Create an event")
        combined = self.client.get(reverse("events:list"), {"view": "upcoming"})
        self.assertContains(combined, self.data["title"])

    def test_kid_cannot_create_even_with_global_model_permission(self):
        self.kid.user_permissions.add(Permission.objects.get(content_type__app_label="events", codename="add_event"))
        token = self.token()
        self.client.force_login(self.kid)
        self.assertEqual(self.client.get(self.create_url).status_code, 404)
        self.assertEqual(self.client.post(self.create_url, {**self.data, "submission_token": token}).status_code, 404)
        self.assertFalse(Event.objects.exists())

    def test_other_families_and_pending_members_cannot_view_events(self):
        self.save_event([self.kid.pk])
        event = Event.objects.get()
        for user in (self.outsider, self.pending):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse("events:detail", args=[event.pk])).status_code, 404)
                self.assertEqual(self.client.get(self.list_url).status_code, 404)
                self.assertEqual(self.client.get(self.create_url).status_code, 404)
                combined = self.client.get(reverse("events:list"), {"view": "upcoming"})
                self.assertNotContains(combined, self.data["title"])
                dashboard = self.client.get(reverse("accounts:dashboard"))
                if dashboard.status_code == 200:
                    self.assertNotContains(dashboard, self.data["title"])

    def test_revoked_membership_removes_event_access(self):
        self.save_event([self.kid.pk])
        event = Event.objects.get()
        Membership.objects.filter(user=self.parent).delete()
        self.assertEqual(self.client.get(reverse("events:detail", args=[event.pk])).status_code, 404)
        self.assertEqual(self.client.get(self.create_url).status_code, 404)
        self.assertEqual(self.client.get(self.list_url).status_code, 404)

    def test_parent_changed_to_kid_cannot_submit_open_form(self):
        token = self.token()
        Membership.objects.filter(user=self.parent).update(role=Membership.Role.KID)
        response = self.client.post(self.create_url, {**self.data, "submission_token": token})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Event.objects.exists())

    def test_retry_does_not_change_saved_participants(self):
        token = self.token()
        response = self.client.post(self.create_url, {
            **self.data, "submission_token": token, "participants": [self.kid.pk],
        })
        retry = self.client.post(self.create_url, {
            **self.data, "title": "Changed retry", "submission_token": token, "participants": [self.admin.pk],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(retry.url, response.url)
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(list(Event.objects.get().participants.all()), [self.kid])
        self.assertEqual(Event.objects.get().title, self.data["title"])

    def test_participant_save_failure_rolls_back_event_and_preserves_choices(self):
        token = self.token()
        data = {**self.data, "submission_token": token, "participants": [self.kid.pk]}
        manager_class = type(Event(pk=1).participants)
        with patch.object(manager_class, "set", side_effect=OperationalError("Temporary failure")):
            response = self.client.post(self.create_url, data)
        self.assertContains(response, "Your details are still here.")
        self.assertEqual(response.context["form"]["participants"].value(), [str(self.kid.pk)])
        self.assertFalse(Event.objects.exists())
        self.assertEqual(self.client.post(self.create_url, data).status_code, 302)
        self.assertEqual(list(Event.objects.get().participants.all()), [self.kid])

    def test_dashboard_links_for_parent_and_kid(self):
        self.save_event([self.kid.pk])
        event = Event.objects.get()
        for user in (self.parent, self.kid):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                dashboard = self.client.get(reverse("accounts:dashboard"))
                self.assertContains(dashboard, reverse("events:detail", args=[event.pk]))
                self.assertContains(dashboard, self.list_url)
                if user == self.parent:
                    self.assertContains(dashboard, self.create_url)
                else:
                    self.assertNotContains(dashboard, self.create_url)

    def test_parent_can_choose_member_workspace(self):
        response = self.client.get(reverse("events:choose_workspace"), {"workspace": self.workspace.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(self.create_url))

    def test_signed_out_event_routes_use_family_login(self):
        self.client.logout()
        for url in (self.create_url, self.list_url, reverse("events:choose_workspace")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_parent_can_sign_in_and_complete_event_flow(self):
        self.client.logout()
        self.assertTrue(self.client.login(username=self.parent.username, password="FamilyTest2026!"))
        response = self.save_event([self.kid.pk])
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(self.list_url, {"view": "upcoming"}), self.data["title"])


    def test_invited_parent_can_create_after_accepting_invitation(self):
        membership = invite_member(
            workspace=self.workspace,
            invited_by=self.admin,
            email="new-parent@example.com",
            role=Membership.Role.PARENT,
        )
        self.client.logout()
        self.assertFalse(self.client.login(username=membership.user.username, password="123456"))
        accept_url = reverse("accounts:accept_invite", args=[make_invite_token(membership)])
        chosen = {"new_password1": "FamilyTest2026!", "new_password2": "FamilyTest2026!"}
        self.assertEqual(self.client.post(accept_url, chosen).status_code, 302)
        self.client.logout()
        login_response = self.client.post(reverse("accounts:login"), {
            "username": membership.user.username, "password": "FamilyTest2026!",
        })
        self.assertEqual(login_response.status_code, 302)
        response = self.save_event([self.kid.pk])
        self.assertEqual(response.status_code, 302)
        event = Event.objects.get()
        self.assertEqual(event.created_by, membership.user)
        self.client.force_login(self.kid)
        self.assertContains(self.client.get(self.list_url, {"view": "upcoming"}), self.data["title"])
