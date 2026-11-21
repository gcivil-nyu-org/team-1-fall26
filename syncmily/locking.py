"""Optimistic locking for rows that two people can edit at once.

Every writer bumps `version`: save() adds one, and every queryset update() must use
F("version") + 1. A stale form edit gets a 409 instead of overwriting newer work.
"""

from django.db import models
from django.db.models import F

SESSION_KEY = "row_versions"
# The session remembers the version each edit page showed, for forms that post no
# hidden field. Only the newest few are kept so the session stays small.
REMEMBERED = 40

CONFLICT_MESSAGE = (
    "Someone changed this while you were editing. Review their change and save again."
)


class VersionedModel(models.Model):
    version = models.PositiveIntegerField(default=0, db_default=0, editable=False)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        bump = (
            not self._state.adding
            and not kwargs.get("force_insert")
            and (update_fields is None or len(update_fields) > 0)
        )
        if bump:
            self.version = F("version") + 1
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "version"}
        super().save(*args, **kwargs)
        if bump:
            self.refresh_from_db(fields=["version"])


def _key(kind, pk):
    return f"{kind}:{pk}"


def remember_version(request, kind, pk, version):
    """Note which version of a row this session last loaded or saved."""
    try:
        seen = dict(request.session.get(SESSION_KEY, {}))
        key = _key(kind, pk)
        seen.pop(key, None)
        seen[key] = int(version)
        while len(seen) > REMEMBERED:
            seen.pop(next(iter(seen)))
        request.session[SESSION_KEY] = seen
    except Exception:  # noqa: BLE001 - a session problem never blocks an edit
        pass


def expected_version(request, kind, pk, posted=None):
    """The version the person edited: the hidden field, else the one this session last
    loaded or saved, else 0 (the version a row has when it is created)."""
    if posted is not None:
        return posted
    try:
        return int(request.session.get(SESSION_KEY, {}).get(_key(kind, pk), 0))
    except (TypeError, ValueError, AttributeError):
        return 0
