from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Workspace
from events.models import Event
from events.access import family_events
from events.timeline import family_members
from syncmily.limits import QUOTAS, RENDER
from syncmily.locking import CONFLICT_MESSAGE, expected_version, remember_version
from syncmily.ratelimit import rate_limit
from syncmily.safety import Params, QuotaExceeded, enforce_quota

from .access import (
    caregiver_workspaces,
    get_manageable_checklist,
    get_manageable_workspace,
    get_viewable_checklist,
    get_viewable_workspace,
)
from .forms import ChecklistForm, ChecklistItemForm, read_submission
from .models import Checklist, ChecklistItem

MAX_ITEMS_PER_CHECKLIST = QUOTAS.items_per_checklist
# A second identical create inside this window is a double click or a retry.
DUPLICATE_WINDOW = timedelta(seconds=30)


class StaleEdit(Exception):
    """The row changed after the edit page was loaded."""


def _visible_event_ids(user, checklists):
    """Ids of the linked events this user may see (kids can't see parent-only plans)."""
    ids = {c.event_id for c in checklists if c.event_id}
    if not ids:
        return set()
    return set(family_events(user).filter(pk__in=ids).values_list("pk", flat=True))


def _event_member(event, members):
    """The family member whose colour a list linked to this event uses, or None."""
    if event is None:
        return None
    people = list(event.participants.all())
    return members.get(people[0].pk) if people else None


# ---------------------------------------------------------
# 1. List all checklists for a family workspace
# ---------------------------------------------------------


@login_required
def checklist_list(request, workspace_id):
    workspace = get_viewable_workspace(request.user, workspace_id)

    checklists = (
        Checklist.objects.filter(workspace=workspace)
        .select_related("event", "created_by")
        .annotate(
            total_items=Count("items"),
            completed_items=Count(
                "items",
                filter=Q(items__is_completed=True),
            ),
        )
        .order_by("-created_at", "pk")
    )

    can_manage = caregiver_workspaces(request.user).filter(pk=workspace.pk).exists()

    # A page shows 50 lists; Paginator treats a bad ?page= as the first or last page.
    page = Paginator(checklists, RENDER.checklists_per_page).get_page(
        Params(request.GET).page()
    )
    # A list linked to an event takes the colour of that event's first person.
    members = {member["user"].pk: member for member in family_members([workspace])}
    shown = list(page.object_list)
    participants = {
        event.pk: event
        for event in Event.objects.filter(
            pk__in={c.event_id for c in shown if c.event_id}
        ).prefetch_related("participants")
    }
    visible = _visible_event_ids(request.user, shown)
    for checklist in shown:
        if checklist.event_id not in visible:
            checklist.event = None
        else:
            checklist.event = participants.get(checklist.event_id, checklist.event)
        checklist.member = _event_member(checklist.event, members)

    return render(
        request,
        "checklists/checklist_list.html",
        {
            "workspace": workspace,
            "checklists": shown,
            "page": page,
            "can_manage": can_manage,
        },
    )


# ---------------------------------------------------------
# 2. Create a new checklist
# ---------------------------------------------------------


def _create_checklist(request, workspace, form):
    """Save the list this form describes, once.

    A signed token (the page carries one) names the list, so a double click or a
    retried post finds the list it already made. A post without a token that repeats
    the same person, family, title and event within 30 seconds gets the same list.
    """
    token_id = read_submission(
        form.cleaned_data.get("submission_token", ""), request.user, workspace
    )
    title, event = form.cleaned_data["title"], form.cleaned_data.get("event")
    with transaction.atomic():
        # One create at a time per family: the same lock the quota check takes.
        Workspace.objects.select_for_update().only("pk").get(pk=workspace.pk)
        if token_id is not None:
            found = Checklist.objects.filter(pk=token_id, workspace=workspace).first()
            if found is not None:
                return found
        found = Checklist.objects.filter(
            workspace=workspace,
            created_by=request.user,
            title=title,
            event=event,
            created_at__gte=timezone.now() - DUPLICATE_WINDOW,
        ).first()
        if found is not None:
            return found
        enforce_quota(workspace, "checklists")
        checklist = form.save(commit=False)
        checklist.workspace = workspace
        checklist.created_by = request.user
        if token_id is not None and not Checklist.objects.filter(pk=token_id).exists():
            checklist.pk = token_id
        checklist.full_clean()
        checklist.save()
        return checklist


@login_required
@rate_limit("checklist_create")
def checklist_create(request, workspace_id):
    workspace = get_manageable_workspace(
        request.user,
        workspace_id,
    )

    if request.method == "POST":
        form = ChecklistForm(
            request.POST,
            workspace=workspace,
            user=request.user,
        )

        if form.is_valid():
            try:
                checklist = _create_checklist(request, workspace, form)
            except QuotaExceeded as error:
                form.add_error(None, error.messages)
            else:
                return redirect(
                    "checklists:detail",
                    checklist_id=checklist.pk,
                )
    else:
        form = ChecklistForm(workspace=workspace, user=request.user)

    return render(
        request,
        "checklists/checklist_form.html",
        {
            "workspace": workspace,
            "form": form,
            "editing": False,
        },
    )


# ---------------------------------------------------------
# 3. View checklist details and completion progress
# ---------------------------------------------------------


@login_required
def checklist_detail(request, checklist_id):
    checklist = get_viewable_checklist(
        request.user,
        checklist_id,
    )

    if checklist.event_id and not _visible_event_ids(request.user, [checklist]):
        checklist.event = None

    items = checklist.items.select_related("completed_by").all()

    total_items = items.count()
    completed_items = items.filter(is_completed=True).count()

    progress = round((completed_items / total_items) * 100) if total_items > 0 else 0

    can_manage = (
        caregiver_workspaces(request.user).filter(pk=checklist.workspace_id).exists()
    )

    members = {
        member["user"].pk: member for member in family_members([checklist.workspace])
    }

    return render(
        request,
        "checklists/checklist_detail.html",
        {
            "checklist": checklist,
            "member": _event_member(checklist.event, members),
            "items": items,
            "total_items": total_items,
            "completed_items": completed_items,
            "progress": progress,
            "can_manage": can_manage,
            "item_form": ChecklistItemForm(),
        },
    )


# ---------------------------------------------------------
# 4. Edit an existing checklist
# ---------------------------------------------------------


@login_required
def checklist_edit(request, checklist_id):
    checklist = get_manageable_checklist(
        request.user,
        checklist_id,
    )
    status = 200
    conflict_message = ""

    if request.method == "POST":
        form = ChecklistForm(
            request.POST,
            instance=checklist,
            workspace=checklist.workspace,
            user=request.user,
        )

        if form.is_valid():
            try:
                with transaction.atomic():
                    current = get_object_or_404(
                        Checklist.objects.select_for_update(), pk=checklist.pk
                    )
                    expected = expected_version(
                        request,
                        "checklist",
                        checklist.pk,
                        form.cleaned_data.get("version"),
                    )
                    if current.version != expected and (
                        current.title != form.cleaned_data["title"]
                        or current.event_id
                        != getattr(form.cleaned_data.get("event"), "pk", None)
                    ):
                        raise StaleEdit
                    checklist = form.save(commit=False)
                    checklist.full_clean()
                    checklist.save()
            except StaleEdit:
                # Show the saved list, so the person can review it and save again.
                conflict_message = CONFLICT_MESSAGE
                checklist.refresh_from_db()
                remember_version(request, "checklist", checklist.pk, checklist.version)
                form = ChecklistForm(
                    instance=checklist,
                    workspace=checklist.workspace,
                    user=request.user,
                )
                status = 409
            else:
                remember_version(request, "checklist", checklist.pk, checklist.version)
                return redirect(
                    "checklists:detail",
                    checklist_id=checklist.pk,
                )
    else:
        remember_version(request, "checklist", checklist.pk, checklist.version)
        form = ChecklistForm(
            instance=checklist,
            workspace=checklist.workspace,
            user=request.user,
        )

    return render(
        request,
        "checklists/checklist_form.html",
        {
            "workspace": checklist.workspace,
            "form": form,
            "editing": True,
            "checklist": checklist,
            "conflict_message": conflict_message,
        },
        status=status,
    )


# ---------------------------------------------------------
# 5. Delete a checklist
# ---------------------------------------------------------


@login_required
@require_POST
def checklist_delete(request, checklist_id):
    checklist = get_manageable_checklist(
        request.user,
        checklist_id,
    )

    workspace_id = checklist.workspace_id

    checklist.delete()

    return redirect(
        "checklists:list",
        workspace_id=workspace_id,
    )


# ---------------------------------------------------------
# 6. Add a new checklist item
# ---------------------------------------------------------


@login_required
@require_POST
@rate_limit("checklist_item_add")
def item_add(request, checklist_id):
    checklist = get_manageable_checklist(
        request.user,
        checklist_id,
    )

    form = ChecklistItemForm(request.POST)

    if form.is_valid():
        with transaction.atomic():
            # Serialize item creation for this checklist. A list deleted a moment ago
            # is a plain 404, never a server error.
            get_object_or_404(Checklist.objects.select_for_update(), pk=checklist.pk)

            try:
                enforce_quota(
                    checklist.workspace_id, "items_per_checklist", checklist=checklist
                )
            except QuotaExceeded:
                messages.error(
                    request,
                    f"A checklist holds up to {MAX_ITEMS_PER_CHECKLIST} items. "
                    "Remove one before adding another.",
                )
            else:
                item = form.save(commit=False)
                item.checklist = checklist

                last_position = (
                    ChecklistItem.objects.filter(checklist=checklist)
                    .order_by("-position")
                    .values_list("position", flat=True)
                    .first()
                )

                item.position = last_position + 1 if last_position is not None else 0

                item.save()
    else:
        messages.error(request, " ".join(form.errors["description"]))

    return redirect(
        "checklists:detail",
        checklist_id=checklist.pk,
    )


# ---------------------------------------------------------
# 7. Edit a checklist item
# ---------------------------------------------------------


@login_required
def item_edit(request, item_id):
    item = get_object_or_404(
        ChecklistItem.objects.select_related("checklist"),
        pk=item_id,
    )

    checklist = get_manageable_checklist(
        request.user,
        item.checklist_id,
    )
    status = 200
    conflict_message = ""

    if request.method == "POST":
        form = ChecklistItemForm(
            request.POST,
            instance=item,
        )

        if form.is_valid():
            try:
                with transaction.atomic():
                    current = get_object_or_404(
                        ChecklistItem.objects.select_for_update(), pk=item.pk
                    )
                    expected = expected_version(
                        request, "item", item.pk, form.cleaned_data.get("version")
                    )
                    if (
                        current.version != expected
                        and current.description != form.cleaned_data["description"]
                    ):
                        raise StaleEdit
                    form.save()
            except StaleEdit:
                conflict_message = CONFLICT_MESSAGE
                item.refresh_from_db()
                remember_version(request, "item", item.pk, item.version)
                form = ChecklistItemForm(instance=item)
                status = 409
            else:
                remember_version(request, "item", item.pk, item.version)
                return redirect(
                    "checklists:detail",
                    checklist_id=checklist.pk,
                )
    else:
        remember_version(request, "item", item.pk, item.version)
        form = ChecklistItemForm(instance=item)

    return render(
        request,
        "checklists/item_form.html",
        {
            "form": form,
            "checklist": checklist,
            "item": item,
            "conflict_message": conflict_message,
        },
        status=status,
    )


# ---------------------------------------------------------
# 8. Delete a checklist item
# ---------------------------------------------------------


@login_required
@require_POST
def item_delete(request, item_id):
    item = get_object_or_404(
        ChecklistItem.objects.select_related("checklist"),
        pk=item_id,
    )

    checklist = get_manageable_checklist(
        request.user,
        item.checklist_id,
    )

    item.delete()

    return redirect(
        "checklists:detail",
        checklist_id=checklist.pk,
    )


# ---------------------------------------------------------
# 9. Mark an item complete or incomplete
# ---------------------------------------------------------


@login_required
@require_POST
def item_toggle(request, item_id):
    wanted = Params(request.POST).choice("completed", ("0", "1"))
    with transaction.atomic():
        item = get_object_or_404(
            ChecklistItem.objects.select_for_update(),
            pk=item_id,
        )

        checklist = get_viewable_checklist(
            request.user,
            item.checklist_id,
        )

        if wanted is not None:
            item.is_completed = wanted == "1"
        else:
            item.is_completed = not item.is_completed

        if item.is_completed:
            item.completed_by = request.user
            item.completed_at = timezone.now()
        else:
            item.completed_by = None
            item.completed_at = None

        # save() adds one to the version, so an open edit page for this item is stale.
        item.save(
            update_fields=[
                "is_completed",
                "completed_by",
                "completed_at",
            ]
        )

    return redirect(
        "checklists:detail",
        checklist_id=checklist.pk,
    )
