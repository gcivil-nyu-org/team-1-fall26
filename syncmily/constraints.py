"""Database CHECK constraints that back the text and data rules (spec 8.5, 10.1).

The app normalizes and validates every write, so these checks never fire for code
that goes through the models. They catch the writers that skip that code: raw SQL,
a future bulk path or a bug. Models list them in Meta.constraints, and each app adds
them in three migrations: normalize the rows, ADD CONSTRAINT ... NOT VALID, then
VALIDATE CONSTRAINT.

The patterns use \\uXXXX escapes, which both Python's re and Postgres regular
expressions read, so the same text works in a migration, in Postgres and in tests.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.db import models
from django.db.models.lookups import LessThanOrEqual, Range

from syncmily import limits

# C0 and C1 controls, the Arabic letter mark, zero-width space, LRM and RLM, the line
# and paragraph separators, bidi embeds and overrides, U+2060-U+206F (word joiner,
# invisible operators, bidi isolates) and the BOM. U+200C (ZWNJ) stays legal: the
# normalizer keeps it between two letters for Persian and Indic spelling (10.1).
# U+200D (ZWJ) stays legal for emoji. Tag characters are left to the app, so
# subdivision flags keep working.
_BAD_CHARS = r"\u007f-\u009f\u061c\u200b\u200e\u200f\u2028-\u202e\u2060-\u206f\ufeff"
# Four or more marks in a row from the generic combining-diacritic blocks. The
# normalizer keeps at most limits.GENERIC_MARKS_PER_BASE of them on one letter.
_MARK_FLOOD = (
    r"[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]"
    "{%d,}" % (limits.GENERIC_MARKS_PER_BASE + 1)
)
TEXT_PATTERN = r"[\u0001-\u001f" + _BAD_CHARS + "]|" + _MARK_FLOOD
# Chat keeps tab and line feed.
MULTILINE_TEXT_PATTERN = (
    r"[\u0001-\u0008\u000b-\u001f" + _BAD_CHARS + "]|" + _MARK_FLOOD
)

HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"
# At least one character that is not white space.
NOT_BLANK_PATTERN = r"[^[:space:]]"

# The app checks the 1900-2100 window in the plan's own time zone. Stored times are
# UTC, so the database bounds sit one day wider and never refuse what the app allows.
EVENT_EARLIEST = datetime(
    limits.EVENT_FIRST_DAY.year, 1, 1, tzinfo=dt_timezone.utc
) - timedelta(days=1)
EVENT_LATEST = datetime(
    limits.EVENT_LAST_DAY.year + 1, 1, 1, tzinfo=dt_timezone.utc
) + timedelta(days=1)
EVENT_MAX_LENGTH = timedelta(days=limits.QUOTAS.event_max_days)


class DatabaseCheck(models.CheckConstraint):
    """A CHECK that only Postgres enforces.

    Django also runs a model's check constraints in full_clean(), one query each,
    and reports a failure as a generic "Constraint ... is violated." form error.
    These checks back up the normalizer and the form policies, which already give
    people a clear message, so model forms skip them and keep their query count.
    """

    def validate(self, model, instance, exclude=None, using=None):
        return None


class CharLength(models.Func):
    """char_length(text): the length in characters, as spec 8.5 writes it."""

    function = "CHAR_LENGTH"
    output_field = models.IntegerField()


def clean_text(model, field, *, multiline=False):
    """The column holds no control, bidi or invisible character and no mark flood."""
    pattern = MULTILINE_TEXT_PATTERN if multiline else TEXT_PATTERN
    return DatabaseCheck(
        condition=~models.Q(**{f"{field}__regex": pattern}),
        name=f"{model}_{field}_clean_text",
    )


def not_blank(model, field):
    """The column holds at least one character that is not white space."""
    return DatabaseCheck(
        condition=models.Q(**{f"{field}__regex": NOT_BLANK_PATTERN}),
        name=f"{model}_{field}_not_blank",
    )


def length_between(model, field, low, high):
    """char_length(field) BETWEEN low AND high."""
    return DatabaseCheck(
        condition=models.Q(Range(CharLength(field), (low, high))),
        name=f"{model}_{field}_length",
    )


def max_length(model, field, high):
    """char_length(field) <= high."""
    return DatabaseCheck(
        condition=models.Q(LessThanOrEqual(CharLength(field), high)),
        name=f"{model}_{field}_length",
    )


def coordinates(model, latitude, longitude):
    """Both coordinates or neither, each in range (NaN fails BETWEEN in Postgres)."""
    return [
        DatabaseCheck(
            condition=(
                models.Q(**{f"{latitude}__isnull": True, f"{longitude}__isnull": True})
                | models.Q(
                    **{
                        f"{latitude}__range": (-90, 90),
                        f"{longitude}__range": (-180, 180),
                    }
                )
            ),
            name=f"{model}_coordinates_in_range",
        )
    ]


def event_window(model):
    """Plans sit inside 1900-2100 (one day of slack each side, see above), and a plan
    made in Syncmily lasts at most limits.QUOTAS.event_max_days. Imported plans keep
    their length (spec 10.1: imports are bounded per connection instead)."""
    return [
        DatabaseCheck(
            condition=models.Q(starts_at__gte=EVENT_EARLIEST, ends_at__lt=EVENT_LATEST),
            name=f"{model}_within_window",
        ),
        DatabaseCheck(
            condition=(
                ~models.Q(source="syncmily")
                | models.Q(ends_at__lte=models.F("starts_at") + EVENT_MAX_LENGTH)
            ),
            name=f"{model}_max_length",
        ),
    ]
