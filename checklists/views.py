
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from events.timeline import family_members

from .access import (
    caregiver_workspaces,
    get_manageable_checklist,
    get_manageable_workspace,
    get_viewable_checklist,
    get_viewable_workspace,
)
from .forms import ChecklistForm, ChecklistItemForm
from .models import Checklist, ChecklistItem


def _event_member(event, members):
    """The family member whose colour a list linked to this event is drawn in, or None."""
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
        .order_by("-created_at")
    )

    can_manage = caregiver_workspaces(request.user).filter(
        pk=workspace.pk
    ).exists()

    # A list linked to an event takes the colour of that event's first person.
    members = {member["user"].pk: member for member in family_members([workspace])}
    checklists = list(checklists.prefetch_related("event__participants"))
    for checklist in checklists:
        checklist.member = _event_member(checklist.event, members)

    return render(
        request,
        "checklists/checklist_list.html",
        {
            "workspace": workspace,
            "checklists": checklists,
            "can_manage": can_manage,
        },
    )


# ---------------------------------------------------------
# 2. Create a new checklist
# ---------------------------------------------------------

@login_required
def checklist_create(request, workspace_id):
    workspace = get_manageable_workspace(
        request.user,
        workspace_id,
    )

    if request.method == "POST":
        form = ChecklistForm(
            request.POST,
            workspace=workspace,
        )

        if form.is_valid():
            checklist = form.save(commit=False)
            checklist.workspace = workspace
            checklist.created_by = request.user

            checklist.full_clean()
            checklist.save()

            return redirect(
                "checklists:detail",
                checklist_id=checklist.pk,
            )
    else:
        form = ChecklistForm(workspace=workspace)

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

    items = checklist.items.select_related(
        "completed_by"
    ).all()

    total_items = items.count()
    completed_items = items.filter(
        is_completed=True
    ).count()

    progress = (
        round((completed_items / total_items) * 100)
        if total_items > 0
        else 0
    )

    can_manage = caregiver_workspaces(request.user).filter(
        pk=checklist.workspace_id
    ).exists()

    members = {member["user"].pk: member for member in family_members([checklist.workspace])}

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

    if request.method == "POST":
        form = ChecklistForm(
            request.POST,
            instance=checklist,
            workspace=checklist.workspace,
        )

        if form.is_valid():
            checklist = form.save(commit=False)

            checklist.full_clean()
            checklist.save()

            return redirect(
                "checklists:detail",
                checklist_id=checklist.pk,
            )
    else:
        form = ChecklistForm(
            instance=checklist,
            workspace=checklist.workspace,
        )

    return render(
        request,
        "checklists/checklist_form.html",
        {
            "workspace": checklist.workspace,
            "form": form,
            "editing": True,
            "checklist": checklist,
        },
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
def item_add(request, checklist_id):
    checklist = get_manageable_checklist(
        request.user,
        checklist_id,
    )

    form = ChecklistItemForm(request.POST)

    if form.is_valid():
        with transaction.atomic():
            # Serialize item creation for this checklist.
            Checklist.objects.select_for_update().get(
                pk=checklist.pk
            )

            item = form.save(commit=False)
            item.checklist = checklist

            last_position = (
                ChecklistItem.objects.filter(
                    checklist=checklist
                )
                .order_by("-position")
                .values_list("position", flat=True)
                .first()
            )

            item.position = (
                last_position + 1
                if last_position is not None
                else 0
            )

            item.save()

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

    if request.method == "POST":
        form = ChecklistItemForm(
            request.POST,
            instance=item,
        )

        if form.is_valid():
            form.save()

            return redirect(
                "checklists:detail",
                checklist_id=checklist.pk,
            )
    else:
        form = ChecklistItemForm(instance=item)

    return render(
        request,
        "checklists/item_form.html",
        {
            "form": form,
            "checklist": checklist,
            "item": item,
        },
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
    with transaction.atomic():
        item = get_object_or_404(
            ChecklistItem.objects.select_for_update(),
            pk=item_id,
        )

        checklist = get_viewable_checklist(
            request.user,
            item.checklist_id,
        )

        item.is_completed = not item.is_completed

        if item.is_completed:
            item.completed_by = request.user
            item.completed_at = timezone.now()
        else:
            item.completed_by = None
            item.completed_at = None

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
