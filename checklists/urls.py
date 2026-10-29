from django.urls import path

from . import views

app_name = "checklists"

urlpatterns = [
    path(
        "workspaces/<uuid:workspace_id>/",
        views.checklist_list,
        name="list",
    ),
    path(
        "workspaces/<uuid:workspace_id>/create/",
        views.checklist_create,
        name="create",
    ),
    path(
        "<uuid:checklist_id>/",
        views.checklist_detail,
        name="detail",
    ),
    path(
        "<uuid:checklist_id>/edit/",
        views.checklist_edit,
        name="edit",
    ),
    path(
        "<uuid:checklist_id>/delete/",
        views.checklist_delete,
        name="delete",
    ),
    path(
        "<uuid:checklist_id>/items/add/",
        views.item_add,
        name="item_add",
    ),
    path(
        "items/<int:item_id>/edit/",
        views.item_edit,
        name="item_edit",
    ),
    path(
        "items/<int:item_id>/delete/",
        views.item_delete,
        name="item_delete",
    ),
    path(
        "items/<int:item_id>/toggle/",
        views.item_toggle,
        name="item_toggle",
    ),
]
