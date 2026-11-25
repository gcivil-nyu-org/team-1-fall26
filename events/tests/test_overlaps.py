"""Overlaps between one person's plans: the counts and names the pages show match the
plain pair-by-pair answer, and a week of 2,000 plans is still quick.
"""

import os
import random
import timeit
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from ..drives import overlap_titles, overlapping

MONDAY = datetime(2026, 10, 26, tzinfo=timezone.utc)
WEEK_MINUTES = 7 * 24 * 60


def plan(pk, start_minute, minutes):
    starts_at = MONDAY + timedelta(minutes=start_minute)
    return SimpleNamespace(
        pk=pk,
        title=f"Plan {pk}",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=minutes),
    )


class OverlapTests(SimpleTestCase):
    def test_counts_and_names_match_every_pair_checked_one_by_one(self):
        rng = random.Random(8)
        for case in range(300):
            plans = [
                plan(pk, 15 * rng.randrange(40), 15 * rng.randrange(1, 12))
                for pk in rng.sample(range(100), rng.randrange(12))
            ]
            ordered = sorted(plans, key=lambda item: (item.starts_at, item.pk))
            earlier = {item.pk: 0 for item in plans}
            later = {item.pk: 0 for item in plans}
            names = {}
            for index, first in enumerate(ordered):
                for second in ordered[index + 1 :]:
                    if second.starts_at < first.ends_at:
                        later[first.pk] += 1
                        earlier[second.pk] += 1
                        names.setdefault(first.pk, []).append(second.title)
                        names.setdefault(second.pk, []).append(first.title)
            with self.subTest(case=case):
                self.assertEqual(
                    overlapping(plans),
                    [(item, earlier[item.pk], later[item.pk]) for item in ordered],
                )
                self.assertEqual(
                    overlap_titles(plans, 3),
                    {
                        pk: titles[:3]
                        + ([f"{len(titles) - 3} more"] if len(titles) > 3 else [])
                        for pk, titles in names.items()
                    },
                )

    def test_a_week_of_2000_plans_takes_well_under_a_second(self):
        shapes = {
            "all overlapping": [plan(pk, pk, WEEK_MINUTES - pk) for pk in range(2000)],
            "none overlapping": [plan(pk, 5 * pk, 5) for pk in range(2000)],
        }
        for shape, plans in shapes.items():
            with self.subTest(shape=shape):
                clashing = 2000 if shape == "all overlapping" else 0
                self.assertEqual(len(overlap_titles(plans, 3)), clashing)
                self.assertEqual(
                    sum(1 for _, earlier, _ in overlapping(plans) if earlier),
                    max(clashing - 1, 0),
                )
                # A shared CI runner is not a performance baseline: CI_SLOW=1 skips it.
                if os.environ.get("CI_SLOW") != "1":
                    quickest = min(
                        timeit.repeat(
                            lambda: (overlapping(plans), overlap_titles(plans, 3)),
                            number=1,
                            repeat=3,
                        )
                    )
                    self.assertLess(quickest, 0.1)
