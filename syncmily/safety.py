"""Text policies, validators, form and model fields, request parsing and quotas.

Views, forms, models, the chat consumer and the importers use these instead of their
own checks. Hardening spec section 2.3 (with section 10) describes each piece;
syncmily/text.py holds the Unicode rules and syncmily/limits.py every number.
"""

import json
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from django import forms
from django.apps import apps
from django.core.exceptions import (
    BadRequest,
    FieldDoesNotExist,
    ImproperlyConfigured,
    ValidationError,
)
from django.core.validators import (
    MaxLengthValidator,
    MinLengthValidator,
    ProhibitNullCharactersValidator,
)
from django.db import connection, models
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from syncmily import limits
from syncmily.text import (
    ascii_skeleton,
    has_mark_flood,
    has_markup,
    is_malformed,
    normalize_text,
    truncate_graphemes,
)

# ---------------------------------------------------------------------------
# Text policies (2.3, 3.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinLength:
    """A policy's extra check: at least `length` characters after normalization."""

    length: int

    def __call__(self, value):
        if len(value) < self.length:
            raise ValidationError(
                f"Enter at least {self.length} characters.", code="too_short"
            )


@dataclass(frozen=True)
class TextPolicy:
    name: str
    max_length: int  # measured after normalization
    multiline: bool = False
    required: bool = True
    reject_markup: bool = True  # forms, chat and raw reads
    reject_mark_flood: bool = True  # forms reject; normalizer-only paths trim
    nfkc: bool = False
    extra: tuple = ()  # more validators (usernames, emails)
    # Take max_length from the field that uses the policy (TITLE fits each title
    # column; LABEL fits both label columns).
    fit_field: bool = False

    @property
    def raw_limit(self):
        """The longest raw input validate_text reads before any Unicode work."""
        return limits.RAW_TEXT_FACTOR * self.max_length + limits.RAW_TEXT_SLACK


TEXT = limits.TEXT
POLICIES = {
    "TITLE": TextPolicy("TITLE", TEXT.event_title, fit_field=True),
    "NAME": TextPolicy("NAME", TEXT.family_name),
    "PLACE": TextPolicy("PLACE", TEXT.event_location, required=False),
    "LABEL": TextPolicy("LABEL", TEXT.trip_label, required=False, fit_field=True),
    "SEARCH": TextPolicy(
        "SEARCH",
        TEXT.search,
        reject_markup=False,  # a search box, never stored
        reject_mark_flood=False,
        extra=(MinLength(TEXT.search_min),),
    ),
    "CHAT": TextPolicy(
        "CHAT",
        TEXT.chat_message,
        multiline=True,
        reject_mark_flood=False,  # chat trims floods (owner decision 9.6)
    ),
    "DESCRIPTION": TextPolicy(
        "DESCRIPTION",
        TEXT.description,
        multiline=True,
        extra=(MinLength(TEXT.description_min),),
    ),
    # The username charset rule joins `extra` with the username validators.
    "USERNAME": TextPolicy(
        "USERNAME", TEXT.username, nfkc=True, extra=(MinLength(TEXT.username_min),)
    ),
    "SHORT_TEXT": TextPolicy("SHORT_TEXT", 255, fit_field=True),
}


def get_policy(policy, *, max_length=None, required=None):
    """The TextPolicy for a name (or a policy), fitted to a field.

    max_length replaces the policy's own for policies that fit their field, and caps
    it for the others. required replaces the policy's flag.
    """
    if isinstance(policy, str):
        try:
            policy = POLICIES[policy]
        except KeyError:
            raise ImproperlyConfigured(f"Unknown text policy {policy!r}.") from None
    changes = {}
    if max_length is not None:
        changes["max_length"] = (
            max_length if policy.fit_field else min(max_length, policy.max_length)
        )
    if required is not None:
        changes["required"] = required
    return replace(policy, **changes) if changes else policy


def validate_text(raw, policy):
    """Check and normalize one piece of interactive text. Returns the clean text.

    The checks run in this order and the first failure raises ValidationError:
    type, raw length, NUL or lone surrogates, mark floods, markup, required, length
    after normalization, then the policy's extra validators.
    """
    policy = get_policy(policy)
    too_long = f"Keep this under {policy.max_length} characters."
    if not isinstance(raw, str):
        raise ValidationError("This value is not text.", code="not_text")
    if len(raw) > policy.raw_limit:
        raise ValidationError(too_long, code="too_long")
    if is_malformed(raw):
        raise ValidationError(
            "This text has characters that are not allowed.", code="malformed"
        )
    if policy.reject_mark_flood and has_mark_flood(
        raw, multiline=policy.multiline, nfkc=policy.nfkc
    ):
        raise ValidationError("Too many accent marks on one letter.", code="mark_flood")
    value = normalize_text(raw, multiline=policy.multiline, nfkc=policy.nfkc)
    if policy.reject_markup and has_markup(value):
        raise ValidationError(
            "Remove the HTML or image code from this text.", code="markup"
        )
    if policy.required and not value:
        raise ValidationError("Enter some visible text.", code="required")
    if len(value) > policy.max_length:
        raise ValidationError(too_long, code="too_long")
    if value:
        for validator in policy.extra:
            validator(value)
    return value


def clean_for_column(value, max_length=None, *, multiline=False, nfkc=False):
    """normalize_text for a write that must not fail: the model fields and signals.

    NFC turns a few characters into longer sequences. When the raw value fitted the
    column, the clean one is cut to fit too, so normalizing never breaks a save.
    """
    if value is None or hasattr(value, "resolve_expression"):
        return value
    raw = value if isinstance(value, str) else str(value)
    clean = normalize_text(raw, multiline=multiline, nfkc=nfkc)
    if max_length and len(clean) > max_length >= len(raw):
        clean = truncate_graphemes(clean, max_length)
    return clean


# ---------------------------------------------------------------------------
# Form fields and the form mixin (2.3)
# ---------------------------------------------------------------------------


class SafeCharField(forms.CharField):
    """A CharField that runs validate_text with a policy and takes its max_length."""

    def __init__(self, *, policy="SHORT_TEXT", **kwargs):
        base = get_policy(policy, max_length=kwargs.pop("max_length", None))
        kwargs.setdefault("required", base.required)
        self.policy = replace(base, required=kwargs["required"])
        super().__init__(max_length=self.policy.max_length, **kwargs)

    def to_python(self, value):
        if value in self.empty_values or (isinstance(value, str) and not value.strip()):
            return self.empty_value
        return validate_text(value, self.policy)


class SafeTextField(SafeCharField):
    """The multiline variant, for longer text such as the AI description."""

    widget = forms.Textarea

    def __init__(self, *, policy="DESCRIPTION", **kwargs):
        super().__init__(policy=policy, **kwargs)


_BUILT_IN_CHECKS = (
    MaxLengthValidator,
    MinLengthValidator,
    ProhibitNullCharactersValidator,
)


class SafeFormMixin:
    """Put text policies on a Form or ModelForm. List it before the Django base:

        class EventForm(SafeFormMixin, forms.Form):
            class Meta:
                text_policies = {"title": "TITLE", "location": "PLACE"}

    It swaps each plain CharField (not a password, choice, email or hidden field)
    for a SafeCharField with the declared policy, or "SHORT_TEXT" when none is
    declared; the widget gets a maxlength. It also cuts any submitted text just past
    the raw length validate_text reads, so a refused form never echoes a huge value.
    The cut value still fails validation, so nobody's text is shortened in silence.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        policies = getattr(getattr(self, "Meta", None), "text_policies", None) or {}
        for name, current in list(self.fields.items()):
            declared = policies.get(name)
            if isinstance(current, SafeCharField):
                if declared:
                    self.fields[name] = self._safe_field(current, declared)
            elif self._is_plain_text(current):
                self.fields[name] = self._safe_field(current, declared or "SHORT_TEXT")
        if self.is_bound:
            self.data = self._bounded_data(self.data)

    @staticmethod
    def _is_plain_text(current):
        return type(current) is forms.CharField and not isinstance(
            current.widget, (forms.PasswordInput, forms.HiddenInput)
        )

    @staticmethod
    def _safe_field(current, policy):
        field_class = (
            SafeTextField
            if isinstance(current.widget, forms.Textarea)
            else SafeCharField
        )
        return field_class(
            policy=policy,
            max_length=current.max_length,
            min_length=current.min_length,
            required=current.required,
            label=current.label,
            initial=current.initial,
            help_text=current.help_text,
            error_messages=current.error_messages,
            widget=current.widget,
            disabled=current.disabled,
            label_suffix=current.label_suffix,
            show_hidden_initial=current.show_hidden_initial,
            localize=current.localize,
            empty_value=current.empty_value,
            validators=[
                check
                for check in current.validators
                if not isinstance(check, _BUILT_IN_CHECKS)
            ],
        )

    def _bounded_data(self, data):
        caps = {
            self.add_prefix(name): current.policy.raw_limit + 1
            for name, current in self.fields.items()
            if isinstance(current, SafeCharField)
        }
        too_long = [
            key
            for key, cap in caps.items()
            if any(
                isinstance(value, str) and len(value) > cap
                for value in (
                    data.getlist(key) if hasattr(data, "getlist") else [data.get(key)]
                )
            )
        ]
        if not too_long:
            return data
        data = data.copy()
        for key in too_long:
            cap = caps[key]
            if hasattr(data, "setlist"):
                data.setlist(
                    key,
                    [v[:cap] if isinstance(v, str) else v for v in data.getlist(key)],
                )
            else:
                data[key] = data[key][:cap]
        return data


# ---------------------------------------------------------------------------
# Model fields (2.3)
# ---------------------------------------------------------------------------


class _CleanTextMixin:
    plain_path = ""

    def __init__(self, *args, policy="SHORT_TEXT", **kwargs):
        self.text_policy = policy
        super().__init__(*args, **kwargs)

    @property
    def policy(self):
        return get_policy(self.text_policy, max_length=self.max_length)

    def normalize(self, value):
        """The clean form this field stores, cut to fit when normalizing lengthened a
        value that fitted (see clean_for_column). Model.clean() methods use it too."""
        policy = self.policy
        return clean_for_column(
            value, self.max_length, multiline=policy.multiline, nfkc=policy.nfkc
        )

    def pre_save(self, model_instance, add):
        """Normalize on every save, so every writer stores the same clean text.

        The clean value is also set on the instance, so code that reads it after the
        save (a serializer, a redirect message) sees what the database holds.
        """
        value = self.normalize(getattr(model_instance, self.attname))
        setattr(model_instance, self.attname, value)
        return value

    def formfield(self, **kwargs):
        form_class = kwargs.get("form_class") or (
            SafeTextField if self.policy.multiline else SafeCharField
        )
        if isinstance(form_class, type) and issubclass(form_class, SafeCharField):
            kwargs.setdefault("policy", self.text_policy)
        kwargs["form_class"] = form_class
        return super().formfield(**kwargs)

    def deconstruct(self):
        # Migrations see the plain Django field: the swap changes no schema.
        name, _, args, kwargs = super().deconstruct()
        return name, self.plain_path, args, kwargs


class CleanCharField(_CleanTextMixin, models.CharField):
    """A CharField that stores normalize_text output (single-line unless the policy
    is multiline) and gives forms and the admin a SafeCharField."""

    plain_path = "django.db.models.CharField"


class CleanTextField(_CleanTextMixin, models.TextField):
    """The TextField variant of CleanCharField."""

    plain_path = "django.db.models.TextField"


class CleanTextQuerySet(models.QuerySet):
    """A QuerySet whose update() and bulk_update() normalize the Clean fields.

    save() and bulk_create() already normalize through the field's pre_save. These
    two writers skip pre_save, so without this a plain .update(name=...) would store
    raw text or fail the database CHECK (spec 10.2, admin and bulk writes). Models
    with Clean fields use CleanTextQuerySet.as_manager() as their manager.
    """

    def _clean_field(self, name):
        try:
            field = self.model._meta.get_field(name)
        except FieldDoesNotExist:  # "pk" and the like are not Clean fields
            return None
        return field if isinstance(field, _CleanTextMixin) else None

    def update(self, **kwargs):
        for name, value in kwargs.items():
            field = self._clean_field(name)
            if field is not None:
                kwargs[name] = field.normalize(value)
        return super().update(**kwargs)

    update.alters_data = True

    def bulk_update(self, objs, fields, batch_size=None):
        clean = [f for f in map(self._clean_field, fields) if f is not None]
        if clean:
            objs = list(objs)
            for obj in objs:
                for field in clean:
                    setattr(
                        obj, field.attname, field.normalize(getattr(obj, field.attname))
                    )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    bulk_update.alters_data = True


# ---------------------------------------------------------------------------
# Raw request reads (2.4)
# ---------------------------------------------------------------------------

_INTEGER = re.compile(r"-?[0-9]{1,19}")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_DECIMAL = re.compile(
    r"-?(?:[0-9]{1,20}(?:\.[0-9]{0,20})?|\.[0-9]{1,20})(?:[eE][-+]?[0-9]{1,3})?"
)
_URL_CHARS = re.compile(r"[!-~]+")  # printable ASCII, no spaces or controls


@lru_cache(maxsize=1)
def _timezone_names():
    return frozenset(available_timezones())


def safe_next_url(request, url, default=""):
    """Return url when it leads back into this site, else default.

    It refuses other hosts, scheme-relative and backslash URLs, controls, spaces and
    anything that is not printable ASCII (Django's reverse() never produces those).
    """
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 2048
        or "\\" in url
        or not _URL_CHARS.fullmatch(url)
        # A bare word ("foo") passes the host check as a relative URL, but
        # redirect() reads it as a view name and raises NoReverseMatch (a 500).
        or not url.startswith(("/", "http://", "https://"))
    ):
        return default
    if url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return default


class Params:
    """Typed reads of request.GET or request.POST.

    Each accessor returns its default when the parameter is missing or empty, and
    raises django.core.exceptions.BadRequest (a 400) when the value is malformed. It
    never echoes the value back.
    """

    def __init__(self, data, request=None):
        self.data = data
        self.request = request

    def _get(self, name):
        value = self.data.get(name)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise BadRequest(f"The {name} value is not valid.")
        return value

    def _bad(self, name):
        return BadRequest(f"The {name} value is not valid.")

    def choice(self, name, allowed, default=None):
        value = self._get(name)
        if value is None:
            return default
        if value not in frozenset(allowed):
            raise self._bad(name)
        return value

    def int(self, name, lo, hi, default=None):
        value = self._get(name)
        if value is None:
            return default
        if not _INTEGER.fullmatch(value) or (value.startswith("-") and lo >= 0):
            raise self._bad(name)
        number = int(value)
        if not lo <= number <= hi:
            raise self._bad(name)
        return number

    def date(self, name, default=None):
        value = self._get(name)
        if value is None:
            return default
        if not _ISO_DATE.fullmatch(value):
            raise self._bad(name)
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise self._bad(name) from None
        if not limits.EVENT_FIRST_DAY <= day <= limits.EVENT_LAST_DAY:
            raise self._bad(name)
        return day

    def time(self, name, default=None):
        value = self._get(name)
        if value is None:
            return default
        match = _CLOCK.fullmatch(value)
        if not match:
            raise self._bad(name)
        return time(int(match.group(1)), int(match.group(2)))

    def float(self, name, lo, hi, default=None):
        value = self._get(name)
        if value is None:
            return default
        if not _DECIMAL.fullmatch(value):
            raise self._bad(name)
        number = float(value)
        if not math.isfinite(number) or not lo <= number <= hi:
            raise self._bad(name)
        return number

    def timezone(self, name, default=None):
        value = self._get(name)
        if value is None:
            return default
        if len(value) > limits.TEXT.timezone_name or value not in _timezone_names():
            raise self._bad(name)
        return value

    def text(self, name, policy):
        value = self.data.get(name)
        try:
            return validate_text("" if value is None else value, policy)
        except ValidationError as error:
            raise BadRequest(error.messages[0]) from None

    def token(self, name, pattern, max_len, default=None):
        value = self._get(name)
        if value is None:
            return default
        if len(value) > max_len or not re.fullmatch(pattern, value):
            raise self._bad(name)
        return value

    def page(self, name="page"):
        """A page number for Paginator.get_page, which already treats a number past
        the end as the last page. Anything but digits or "last" means page 1."""
        value = self.data.get(name)
        if isinstance(value, str) and (
            value == "last" or re.fullmatch(r"[0-9]{1,9}", value)
        ):
            return value
        return 1

    def next_url(self, name, default=""):
        if self.request is None:
            raise ImproperlyConfigured("Params.next_url needs the request.")
        return safe_next_url(self.request, self.data.get(name), default)


def parse_json_body(body, *, max_bytes=None, max_depth=None):
    """Decode a JSON request body (bytes, str or an HttpRequest). Raises BadRequest
    when it is too big, not JSON or nested too deeply."""
    max_bytes = limits.JSON_BODY_MAX_BYTES if max_bytes is None else max_bytes
    max_depth = limits.JSON_MAX_DEPTH if max_depth is None else max_depth
    if hasattr(body, "body"):
        body = body.body
    if len(body) > max_bytes:
        raise BadRequest("The request body is too large.")
    try:
        value = json.loads(body)
    except (ValueError, RecursionError, UnicodeError):
        raise BadRequest("The request body is not valid JSON.") from None
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > max_depth:
                raise BadRequest("The request body is nested too deeply.")
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return value


# ---------------------------------------------------------------------------
# Dates and colours (2.3)
# ---------------------------------------------------------------------------


def _local(moment, zone):
    if zone is None or timezone.is_naive(moment):
        return moment
    return moment.astimezone(ZoneInfo(zone) if isinstance(zone, str) else zone)


def validate_event_window(starts_at, ends_at, tz=None):
    """Check a plan's start and end: both inside 1900-01-01 to 2100-12-31 in local
    time (tz, else each datetime's own zone), the end after the start, and at most
    limits.QUOTAS.event_max_days long. Raises ValidationError."""
    out_of_range = ValidationError(
        "Choose a date between 1900 and 2100.", code="out_of_range"
    )
    try:
        first, last = _local(starts_at, tz), _local(ends_at, tz)
    except (OverflowError, ValueError):
        raise out_of_range from None
    if first.date() < limits.EVENT_FIRST_DAY or last.date() > limits.EVENT_LAST_DAY:
        raise out_of_range
    if ends_at <= starts_at:
        raise ValidationError(
            "End time must be after start time.", code="end_before_start"
        )
    if ends_at - starts_at > timedelta(days=limits.QUOTAS.event_max_days):
        raise ValidationError(
            f"A plan can last at most {limits.QUOTAS.event_max_days} days.",
            code="too_long",
        )


def clean_coordinates(latitude, longitude):
    """(latitude, longitude) when both are finite and in range, else (None, None).

    A pair with one value missing or out of range cannot place anything on a map,
    and the database checks refuse it (spec 8.5), so writers store neither.
    """
    try:
        if (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            return latitude, longitude
    except TypeError:
        pass
    return None, None


class CleanCoordinatesMixin:
    """For models with a latitude and longitude pair: save() stores a broken pair
    as two nulls instead of failing the database check. List it before the model
    base and name the pairs in coordinate_fields."""

    coordinate_fields = (("latitude", "longitude"),)

    def save(self, *args, **kwargs):
        for latitude, longitude in self.coordinate_fields:
            pair = (getattr(self, latitude), getattr(self, longitude))
            if pair != (None, None):
                cleaned = clean_coordinates(*pair)
                setattr(self, latitude, cleaned[0])
                setattr(self, longitude, cleaned[1])
        super().save(*args, **kwargs)


_HEX_COLOR = re.compile(r"#?([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})")


def safe_hex_color(value, default):
    """Return value as "#rrggbb" when it is a hex colour, else default.

    It accepts the "#rgb" short form and colours without "#" (as routing providers
    send them) and expands them, so nothing but a colour ever reaches style="".
    """
    if isinstance(value, str):
        match = _HEX_COLOR.fullmatch(value.strip())
        if match:
            digits = match.group(1)
            if len(digits) == 3:
                digits = "".join(digit * 2 for digit in digits)
            return f"#{digits}"
    return default


# ---------------------------------------------------------------------------
# Quotas (4.2 with 10.1 and 10.2)
# ---------------------------------------------------------------------------


class QuotaExceeded(ValidationError):
    """A family reached one of the hard caps in limits.QUOTAS."""

    # The positional arguments match ValidationError's, so copying or pickling the
    # error (which rebuilds it from its args) keeps working.
    def __init__(self, message, code="quota", params=None, *, kind=None, limit=None):
        super().__init__(message, code=code, params=params)
        self.kind = kind
        self.limit = limit


def _zone(tz):
    zone = tz or timezone.get_current_timezone()
    return ZoneInfo(zone) if isinstance(zone, str) else zone


def _day_bounds(day, tz):
    zone = _zone(tz)
    start = datetime.combine(day, time.min, zone)
    return start, datetime.combine(day + timedelta(days=1), time.min, zone)


def _user_created_events(workspace_id):
    Event = apps.get_model("events", "Event")
    return Event.objects.filter(workspace_id=workspace_id, source=Event.Source.SYNCMILY)


def _count_events_per_day(workspace_id, *, day=None, tz=None, **_):
    if day is None:
        raise ImproperlyConfigured("The events_per_day quota needs day=.")
    start, end = _day_bounds(day, tz)
    events = _user_created_events(workspace_id)
    return events.filter(starts_at__gte=start, starts_at__lt=end).count()


def _count_events_rolling_30d(workspace_id, *, now=None, **_):
    since = (now or timezone.now()) - timedelta(days=30)
    return _user_created_events(workspace_id).filter(created_at__gte=since).count()


def _count_checklists(workspace_id, **_):
    Checklist = apps.get_model("checklists", "Checklist")
    return Checklist.objects.filter(workspace_id=workspace_id).count()


def _count_items(workspace_id, *, checklist=None, **_):
    if checklist is None:
        raise ImproperlyConfigured("The items_per_checklist quota needs checklist=.")
    ChecklistItem = apps.get_model("checklists", "ChecklistItem")
    return ChecklistItem.objects.filter(
        checklist_id=getattr(checklist, "pk", checklist)
    ).count()


def _count_members(workspace_id, **_):
    Membership = apps.get_model("accounts", "Membership")
    return Membership.objects.filter(workspace_id=workspace_id).count()


def _count_chat_messages(workspace_id, **_):
    ChatMessage = apps.get_model("chat", "ChatMessage")
    return ChatMessage.objects.filter(workspace_id=workspace_id).count()


def _count_chat_messages_today(workspace_id, *, user=None, tz=None, now=None, **_):
    if user is None:
        raise ImproperlyConfigured("The chat_messages_per_user_day quota needs user=.")
    ChatMessage = apps.get_model("chat", "ChatMessage")
    zone = _zone(tz)
    today = timezone.localtime(now or timezone.now(), zone).date()
    start, end = _day_bounds(today, zone)
    return ChatMessage.objects.filter(
        workspace_id=workspace_id,
        sender_id=getattr(user, "pk", user),
        created_at__gte=start,
        created_at__lt=end,
    ).count()


QUOTA_RULES = {
    # kind: (counter, limit, message)
    "events_per_day": (
        _count_events_per_day,
        limits.QUOTAS.events_per_day,
        "This family already has {limit} plans on that day, the most Syncmily keeps "
        "for one day. Move or delete a plan to add another.",
    ),
    "events_rolling_30d": (
        _count_events_rolling_30d,
        limits.QUOTAS.events_per_30_days,
        "This family added {limit:,} plans in the last 30 days, the most Syncmily "
        "allows. Delete plans you no longer need, or add more in a few days.",
    ),
    "checklists": (
        _count_checklists,
        limits.QUOTAS.checklists_per_family,
        "This family has {limit} checklists, the most Syncmily keeps. Delete some old "
        "lists to add a new one.",
    ),
    "items_per_checklist": (
        _count_items,
        limits.QUOTAS.items_per_checklist,
        "This checklist has {limit} items, the most Syncmily keeps. Delete some items "
        "to add new ones.",
    ),
    "members": (
        _count_members,
        limits.QUOTAS.members_per_family,
        "This family has {limit} members and open invites, the most Syncmily allows. "
        "Remove someone or cancel an invite first.",
    ),
    "chat_messages": (
        _count_chat_messages,
        limits.QUOTAS.chat_messages_per_family,
        "This family's chat holds {limit:,} messages, the most Syncmily keeps. "
        "Syncmily clears the oldest messages overnight, so you can send again "
        "tomorrow.",
    ),
    "chat_messages_per_user_day": (
        _count_chat_messages_today,
        limits.QUOTAS.chat_messages_per_user_per_day,
        "You have sent {limit} messages today, the most Syncmily allows in one day. "
        "Try again tomorrow.",
    ),
}


def enforce_quota(workspace, kind, *, extra=1, **context):
    """Raise QuotaExceeded when adding `extra` rows of `kind` would pass the cap.

    Call it inside transaction.atomic(), before the insert. It first locks the
    family's Workspace row with SELECT ... FOR UPDATE, so two saves for one family
    run one after the other and cannot both slip under the cap. Every writer takes
    that lock first, so locks are always taken in the same order.

    Kinds and their context: events_per_day (day=, the plan's local date, and
    tz=), events_rolling_30d (Google imports never count), checklists,
    items_per_checklist (checklist=), members (accepted plus pending),
    chat_messages and chat_messages_per_user_day (user=). Returns the count found.
    """
    if not connection.in_atomic_block:
        raise ImproperlyConfigured(
            "enforce_quota must run inside transaction.atomic()."
        )
    try:
        counter, cap, message = QUOTA_RULES[kind]
    except KeyError:
        raise ImproperlyConfigured(f"Unknown quota {kind!r}.") from None
    Workspace = apps.get_model("accounts", "Workspace")
    workspace_id = getattr(workspace, "pk", workspace)
    Workspace.objects.select_for_update().only("pk").get(pk=workspace_id)
    count = counter(workspace_id, **context)
    if count + extra > cap:
        raise QuotaExceeded(message.format(limit=cap), kind=kind, limit=cap)
    return count


# ---------------------------------------------------------------------------
# Usernames (3.2, 9.1, 10.1)
# ---------------------------------------------------------------------------

# After NFKC: ASCII letters, digits, dot, dash and underscore, 3 to 30 characters.
# This one rule removes Cyrillic and Greek look-alikes, invisible fillers, "@", "+"
# and spaces (H-INVENTORY-10, H-LOGIC-11/12, H-MARKUP-047/048).
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{3,30}")
USERNAME_CHARSET_MESSAGE = (
    "Use 3 to 30 letters (A to Z), digits, dots, dashes or underscores."
)

# Names people may not choose. The invite flow's own Parent2 and Kid3 are made by
# accounts.services.next_member_username and skip this check.
_RESERVED_WORDS = (
    "admin",
    "administrator",
    "root",
    "support",
    "help",
    "security",
    "staff",
    "system",
    "moderator",
    "syncmily",
    "owner",
    "family",
    "noreply",
    "no-reply",
)
_RESERVED_NUMBERED = ("parent", "kid", "caregiver")
RESERVED_USERNAME = re.compile(
    r"^(?:%s|(?:%s)\d*)$"
    % ("|".join(map(re.escape, _RESERVED_WORDS)), "|".join(_RESERVED_NUMBERED)),
    re.IGNORECASE,
)
# The same names compared on their look-alike skeleton, so "adm1n" and "Parentl"
# count as "admin" and "Parent1". Digits that the skeleton maps to letters (0 to o,
# 1 to l) may follow parent, kid and caregiver.
_RESERVED_SKELETON = re.compile(
    r"^(?:%s|(?:%s)[0-9lo]*)$"
    % (
        "|".join(re.escape(ascii_skeleton(word)) for word in _RESERVED_WORDS),
        "|".join(re.escape(ascii_skeleton(word)) for word in _RESERVED_NUMBERED),
    )
)
RESERVED_USERNAME_MESSAGE = (
    "That username is reserved. Choose another one; "
    "family members get names like Parent2 or Kid3 from their invite."
)


def normalize_username(value):
    """NFKC first, as Django's model does on save, so every check sees the stored
    name (H-LOGIC-10, H-MARKUP-046)."""
    return unicodedata.normalize(
        "NFKC", value if isinstance(value, str) else ""
    ).strip()


def is_reserved_username(value):
    name = normalize_username(value)
    return bool(
        RESERVED_USERNAME.match(name) or _RESERVED_SKELETON.match(ascii_skeleton(name))
    )


def username_validators(value, *, chosen=True, family_names=()):
    """The text rules for a username (spec 3.2 rules 1, 2, 3 and 5). Returns the
    normalized name or raises ValidationError.

    chosen=False skips the reserved list, for the names the invite flow makes.
    family_names are the other usernames in the person's family; a name whose
    look-alike skeleton matches one of them is refused ("Mom" and "M0m").
    Global uniqueness and the rename rules need the database and live in
    accounts.services.check_username.
    """
    name = normalize_username(value)
    if not USERNAME_PATTERN.fullmatch(name):
        raise ValidationError(USERNAME_CHARSET_MESSAGE, code="username_charset")
    if chosen and is_reserved_username(name):
        raise ValidationError(RESERVED_USERNAME_MESSAGE, code="username_reserved")
    skeleton = ascii_skeleton(name)
    if any(ascii_skeleton(other) == skeleton for other in family_names):
        raise ValidationError(
            "That name looks too much like someone else's in your family. "
            "Choose a different one.",
            code="username_lookalike",
        )
    return name


def username_from_email(email):
    """A valid username base from an email's local part (10.1 usernames): mapped to
    the username charset, "+" turned into "_", at most 26 characters so a number
    still fits, and "member" when fewer than 3 characters are left."""
    local = normalize_username(email).split("@")[0].replace("+", "_")
    base = re.sub(r"[^A-Za-z0-9._-]", "", local)[:26]
    return base if len(base) >= 3 else "member"
