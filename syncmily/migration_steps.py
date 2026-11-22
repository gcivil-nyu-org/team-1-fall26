"""Steps the CHECK-constraint migrations share (spec 8.5 with 10.1).

Each app adds its constraints in three migrations:

1. a data migration that cleans existing rows with these helpers and logs counts;
2. AddConstraintNotValid, which checks new writes at once without scanning the table;
3. ValidateConstraint, which scans the table under a lock that still allows writes.

The data steps change values in place and cannot be undone: the raw values they
replace are the ones the new checks forbid. Their reverse does nothing, so the
migrations can still be rolled back.
"""

import logging

from django.db import migrations

from syncmily import limits
from syncmily.safety import clean_coordinates, clean_for_column, safe_hex_color
from syncmily.text import truncate_graphemes

logger = logging.getLogger("syncmily.migrations")

# The deploy's database role sets statement_timeout to 25 s. Cleaning and validating
# a large table may take longer, so the heavy migrations lift it for their own
# transaction only.
NO_STATEMENT_TIMEOUT = migrations.RunSQL(
    "SET LOCAL statement_timeout = 0", migrations.RunSQL.noop, elidable=True
)


def noop(apps, schema_editor):
    """Reverse of a data step: nothing to restore."""


def normalize_columns(apps, app_label, model_name, columns):
    """Store normalize_text output in each column.

    columns maps a field name to a dict with max_length, and optionally multiline,
    nfkc and fallback (used when nothing visible is left in a required column).
    Returns how many rows changed.
    """
    model = apps.get_model(app_label, model_name)
    names = list(columns)
    changed = 0
    rows = model.objects.order_by().values_list("pk", *names)
    for pk, *values in rows.iterator(chunk_size=2000):
        updates = {}
        for name, value in zip(names, values):
            spec = columns[name]
            clean = clean_for_column(
                value,
                spec["max_length"],
                multiline=spec.get("multiline", False),
                nfkc=spec.get("nfkc", False),
            )
            if len(clean) > spec["max_length"]:
                clean = truncate_graphemes(clean, spec["max_length"])
            if not clean and spec.get("fallback"):
                clean = spec["fallback"]
            if clean != value:
                updates[name] = clean
        if updates:
            model.objects.filter(pk=pk).update(**updates)
            changed += 1
    logger.info("Cleaned text in %s %s rows.", changed, model._meta.label)
    return changed


def clear_bad_coordinates(apps, app_label, model_name, latitude, longitude):
    """Set both coordinates to null where they are out of range, not finite, or
    only one of the pair is set. Returns how many rows changed."""
    model = apps.get_model(app_label, model_name)
    rows = model.objects.order_by().values_list("pk", latitude, longitude)
    bad = [
        pk
        for pk, lat, lng in rows.iterator()
        if clean_coordinates(lat, lng) != (lat, lng)
    ]
    if bad:
        model.objects.filter(pk__in=bad).update(**{latitude: None, longitude: None})
    logger.info("Cleared coordinates on %s %s rows.", len(bad), model._meta.label)
    return len(bad)


def fix_member_colors(apps):
    """Expand short hex colours to #rrggbb and delete colours that are not hex. A
    member without a row gets the default colour. Returns (fixed, deleted)."""
    model = apps.get_model("accounts", "MemberColor")
    fixed = deleted = 0
    for pk, color in model.objects.order_by().values_list("pk", "color").iterator():
        clean = safe_hex_color(color, None)
        if clean == color:
            continue
        if clean is None:
            model.objects.filter(pk=pk).delete()
            deleted += 1
        else:
            model.objects.filter(pk=pk).update(color=clean)
            fixed += 1
    logger.info("Member colours: %s fixed, %s removed.", fixed, deleted)
    return fixed, deleted


# Spec 9.4: plans keep their place as far as the window allows. The bounds are the
# app's own (1900-01-01 to 2100-12-31 23:59 UTC), inside the database check's slack.
_FIRST = "1900-01-01 00:00:00+00"
_LAST = "2100-12-31 23:59:00+00"


def fit_events_to_window(apps, schema_editor):
    """Cut plans made in Syncmily to at most 90 days, and move plans outside
    1900-2100 to the nearest bound with their length kept. Logs one line per plan.

    It works in SQL because Python cannot load dates past the year 9999.
    """
    longest = "interval '%d days'" % limits.QUOTAS.event_max_days
    too_long = "source = 'syncmily' AND ends_at > starts_at + " + longest
    outside = f"starts_at < '{_FIRST}' OR ends_at > '{_LAST}'"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, starts_at::text, ends_at::text FROM events_event "
            f"WHERE ({too_long}) OR {outside} ORDER BY id"
        )
        moved = cursor.fetchall()
        for pk, starts, ends in moved:
            logger.warning(
                "Event %s (%s to %s) is outside the allowed window or longer than "
                "%s days; moving it.",
                pk,
                starts,
                ends,
                limits.QUOTAS.event_max_days,
            )
        cursor.execute(
            f"UPDATE events_event SET ends_at = starts_at + {longest} WHERE {too_long}"
        )
        cursor.execute(
            f"UPDATE events_event SET ends_at = '{_FIRST}'::timestamptz "
            f"+ (ends_at - starts_at), starts_at = '{_FIRST}' "
            f"WHERE starts_at < '{_FIRST}'"
        )
        cursor.execute(
            "UPDATE events_event SET starts_at = GREATEST("
            f"'{_LAST}'::timestamptz - (ends_at - starts_at), '{_FIRST}'), "
            f"ends_at = '{_LAST}' WHERE ends_at > '{_LAST}'"
        )
    logger.info("Moved %s events into the allowed window.", len(moved))
    return len(moved)


def validate(model_name, *names):
    """ValidateConstraint operations for one model."""
    from django.contrib.postgres.operations import ValidateConstraint

    return [ValidateConstraint(model_name, name) for name in names]
