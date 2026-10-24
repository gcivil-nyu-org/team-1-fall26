from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from .conflicts import (
    UTC, ScheduleItem, conflicting_keys, find_conflicts, group_by_pair, is_all_day, overlap_rule,
)

NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
KID, PARENT = 1, 2


def at(day, hour, minute=0, tz=NEW_YORK):
    return datetime(2026, 10, day, hour, minute, tzinfo=tz)


def fall_back(hour, minute, fold=0):
    """A New York time on 2026-11-01, when clocks go back at 2 AM."""
    return datetime(2026, 11, 1, hour, minute, fold=fold, tzinfo=NEW_YORK)


def item(key, starts_at, ends_at, members=(KID,), household="family", **fields):
    return ScheduleItem(key, starts_at, ends_at, frozenset(members), household=household, **fields)


def pairs(conflicts):
    return [(conflict.member_id, conflict.first, conflict.second) for conflict in conflicts]


class OverlapTests(SimpleTestCase):
    def test_overlapping_events_for_the_same_member_conflict(self):
        conflicts = find_conflicts([
            item("soccer", at(20, 15), at(20, 17)),
            item("piano", at(20, 16), at(20, 18)),
        ])
        self.assertEqual(pairs(conflicts), [(KID, "soccer", "piano")])
        self.assertEqual((conflicts[0].starts_at, conflicts[0].ends_at), (at(20, 16), at(20, 17)))
        self.assertEqual(conflicts[0].rule, "overlap")

    def test_touching_edges_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("soccer", at(20, 15), at(20, 16)),
            item("piano", at(20, 16), at(20, 17)),
        ]), [])

    def test_one_minute_of_overlap_conflicts(self):
        self.assertEqual(len(find_conflicts([
            item("soccer", at(20, 15), at(20, 16, 1)),
            item("piano", at(20, 16), at(20, 17)),
        ])), 1)

    def test_different_members_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("soccer", at(20, 15), at(20, 17), members=[KID]),
            item("meeting", at(20, 15), at(20, 17), members=[PARENT]),
        ]), [])

    def test_events_without_members_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("dinner", at(20, 18), at(20, 20), members=[]),
            item("movie", at(20, 19), at(20, 21), members=[]),
        ]), [])

    def test_each_shared_member_is_reported_and_grouped_by_pair(self):
        conflicts = find_conflicts([
            item("soccer", at(20, 15), at(20, 17), members=[KID, PARENT]),
            item("piano", at(20, 16), at(20, 18), members=[KID, PARENT]),
        ])
        self.assertCountEqual(pairs(conflicts), [(KID, "soccer", "piano"), (PARENT, "soccer", "piano")])
        grouped = group_by_pair(conflicts)
        self.assertEqual(len(grouped), 1)
        self.assertCountEqual(grouped[0]["member_ids"], [KID, PARENT])

    def test_three_way_overlap_reports_every_pair(self):
        conflicts = find_conflicts([
            item("a", at(20, 9), at(20, 12)),
            item("b", at(20, 10), at(20, 11)),
            item("c", at(20, 10, 30), at(20, 13)),
        ])
        self.assertCountEqual(pairs(conflicts), [(KID, "a", "b"), (KID, "a", "c"), (KID, "b", "c")])
        self.assertEqual(conflicting_keys(conflicts)["a"], {"b", "c"})

    def test_contained_event_conflicts_with_its_container(self):
        conflicts = find_conflicts([
            item("b", at(20, 10), at(20, 11)),
            item("a", at(20, 9), at(20, 17)),
        ])
        self.assertEqual(pairs(conflicts), [(KID, "a", "b")])
        self.assertEqual((conflicts[0].starts_at, conflicts[0].ends_at), (at(20, 10), at(20, 11)))

    def test_identical_times_conflict(self):
        self.assertEqual(len(find_conflicts([
            item("a", at(20, 9), at(20, 10)),
            item("b", at(20, 9), at(20, 10)),
        ])), 1)

    def test_events_across_midnight(self):
        conflicts = find_conflicts([
            item("sleepover", at(20, 23), at(21, 1)),
            item("late-film", at(21, 0, 30), at(21, 2)),
            item("after", at(21, 1), at(21, 1, 15)),
        ])
        self.assertCountEqual(pairs(conflicts), [(KID, "sleepover", "late-film"), (KID, "late-film", "after")])

    def test_compares_instants_not_wall_times_across_timezones(self):
        # 9 AM in Tokyo on Oct 21 is 8 PM on Oct 20 in New York.
        self.assertEqual(len(find_conflicts([
            item("call", at(21, 9, tz=TOKYO), at(21, 10, tz=TOKYO)),
            item("dinner", at(20, 19, 30), at(20, 21)),
        ])), 1)

    def test_dst_fall_back_repeated_hour_is_not_a_false_conflict(self):
        # On 2026-11-01 New York repeats 1-2 AM: fold=0 is EDT, fold=1 is EST an hour later.
        self.assertEqual(find_conflicts([
            item("first-pass", fall_back(1, 0), fall_back(1, 45)),
            item("second-pass", fall_back(1, 15, fold=1), fall_back(1, 45, fold=1)),
        ]), [])

    def test_dst_fall_back_overlap_hidden_by_wall_times_is_found(self):
        # 1:00 EDT to 1:05 EST lasts 65 minutes and contains 1:30-1:50 EDT.
        conflicts = find_conflicts([
            item("long", fall_back(1, 0), fall_back(1, 5, fold=1)),
            item("short", fall_back(1, 30), fall_back(1, 50)),
        ])
        self.assertEqual(pairs(conflicts), [(KID, "long", "short")])

    def test_all_day_events_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("holiday", at(20, 0), at(21, 0), all_day=True),
            item("birthday", at(20, 0), at(21, 0), all_day=True),
            item("dentist", at(20, 9), at(20, 10)),
        ]), [])

    def test_cancelled_events_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("soccer", at(20, 15), at(20, 17), cancelled=True),
            item("piano", at(20, 16), at(20, 18)),
        ]), [])

    def test_households_are_never_compared(self):
        self.assertEqual(find_conflicts([
            item("ours", at(20, 15), at(20, 17), household="ours"),
            item("theirs", at(20, 16), at(20, 18), household="theirs"),
        ]), [])

    def test_results_are_in_time_order(self):
        conflicts = find_conflicts([
            item("late-a", at(22, 9), at(22, 11)),
            item("late-b", at(22, 10), at(22, 12)),
            item("early-a", at(20, 9), at(20, 11)),
            item("early-b", at(20, 10), at(20, 12)),
        ])
        self.assertEqual([conflict.first for conflict in conflicts], ["early-a", "late-a"])

    def test_rules_get_each_members_items_in_time_order(self):
        calls = []

        def travel_rule(member_id, items):
            calls.append((member_id, [entry.key for entry in items], [entry.location for entry in items]))
            return []

        find_conflicts([
            item("piano", at(20, 17), at(20, 18), location="Music school"),
            item("soccer", at(20, 15), at(20, 16), location="Park"),
        ], rules=(overlap_rule, travel_rule))
        self.assertEqual(calls, [(KID, ["soccer", "piano"], ["Park", "Music school"])])


class AllDayTests(SimpleTestCase):
    def test_midnight_to_midnight_is_all_day(self):
        self.assertTrue(is_all_day(at(20, 0), at(21, 0), "America/New_York"))
        self.assertTrue(is_all_day(at(20, 0), at(23, 0), "America/New_York"))

    def test_dst_day_is_still_all_day(self):
        # Nov 1 2026 has 25 hours in New York.
        start = datetime(2026, 11, 1, tzinfo=NEW_YORK)
        end = datetime(2026, 11, 2, tzinfo=NEW_YORK)
        self.assertEqual(end.astimezone(UTC) - start.astimezone(UTC), timedelta(hours=25))
        self.assertTrue(is_all_day(start, end, "America/New_York"))

    def test_timed_events_are_not_all_day(self):
        self.assertFalse(is_all_day(at(20, 0), at(20, 12), "America/New_York"))
        self.assertFalse(is_all_day(at(20, 9), at(21, 9), "America/New_York"))

    def test_uses_the_events_own_timezone(self):
        # Midnight in Tokyo is not midnight in New York.
        start = datetime(2026, 10, 20, tzinfo=TOKYO)
        self.assertTrue(is_all_day(start, start + timedelta(days=1), "Asia/Tokyo"))
        self.assertFalse(is_all_day(start, start + timedelta(days=1), "America/New_York"))

    def test_invalid_timezone_is_not_all_day(self):
        self.assertFalse(is_all_day(at(20, 0), at(21, 0), "Not/AZone"))
