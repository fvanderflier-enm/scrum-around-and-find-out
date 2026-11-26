# SPDX-License-Identifier: Apache-2.0
"""Read the live Azure DevOps project: its process, work item types (states and fields), the organization's fields
and picklists, the area and iteration nodes, the team's iterations and the shared queries board.yaml owns.

Every answer is checked for shape where it is read; a value of the wrong kind is MalformedDataError, never a guess.
States and fields are read through the project (`_apis/wit/workitemtypes`), which works whatever process the project
uses; the process itself is read to say whether bootstrap may change it (an inherited process) or not.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from safo.ado.client import AdoClient, value_list
from safo.ado.config import azure_of, reference_of, role_of
from safo.errors import MalformedDataError, NotFoundError
from safo.schema import Board

PROCESS_PROPERTY = "System.ProcessTemplateType"
SHARED_QUERIES = "Shared Queries"
DONE_CATEGORIES = ("Completed", "Removed")


def text(node: Any, key: str, what: str, *, optional: bool = False) -> str:
    value = node.get(key) if isinstance(node, dict) else None
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise MalformedDataError(f"{what}: {key} is not text")
    return value


def flag(node: Any, key: str) -> bool:
    value = node.get(key) if isinstance(node, dict) else None
    return value is True


def entries(node: Any, key: str, what: str) -> list[Any]:
    """A list under `key`, empty when the key is absent or null; anything else is not shaped as asked."""
    value = node.get(key) if isinstance(node, dict) else None
    if value is None:
        return []
    if not isinstance(value, list):
        raise MalformedDataError(f"{what}: {key} is not a list")
    return value


def day(value: Any, what: str) -> dt.date | None:
    """The calendar day of an Azure DevOps timestamp (`2026-10-20T00:00:00Z`), None for no value."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedDataError(f"{what}: the date is not text")
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        raise MalformedDataError(f"{what}: the date is not YYYY-MM-DD") from None


@dataclass(frozen=True)
class State:
    name: str
    category: str
    color: str


@dataclass(frozen=True)
class WorkItemType:
    name: str
    reference: str
    states: tuple[State, ...]
    fields: frozenset[str]  # reference names, lower case

    def state(self, name: str) -> State | None:
        return next((s for s in self.states if s.name == name), None)

    def has_field(self, reference: str) -> bool:
        return reference.lower() in self.fields


@dataclass(frozen=True)
class OrgField:
    name: str
    reference: str
    type: str  # string | integer | double | dateTime | boolean | html | plainText | treePath | ...
    picklist: str  # the picklist id, or "" when the field has none


@dataclass(frozen=True)
class Node:
    name: str
    path: str
    identifier: str
    start: dt.date | None = None
    finish: dt.date | None = None


@dataclass(frozen=True)
class SharedQuery:
    id: str
    name: str
    wiql: str


@dataclass(frozen=True)
class Process:
    id: str
    name: str
    customization: str  # system | inherited | custom


@dataclass
class AdoLive:
    project_id: str
    project: str
    process: Process
    types: dict[str, WorkItemType] = field(default_factory=dict)  # by the name board.yaml uses
    fields_by_name: dict[str, OrgField] = field(default_factory=dict)  # lower-case display name -> field
    fields_by_ref: dict[str, OrgField] = field(default_factory=dict)  # lower-case reference -> field
    picklists: dict[str, tuple[str, ...]] = field(default_factory=dict)  # id -> items
    areas: dict[str, Node] = field(default_factory=dict)  # the children of the root area, by name
    iterations: dict[str, Node] = field(default_factory=dict)  # the children of the root iteration, by name
    team_iterations: frozenset[str] | None = None  # identifiers the team has selected; None: the team is missing
    queries: dict[str, SharedQuery] | None = None  # None: the folder does not exist

    def org_field(self, name: str, reference: str) -> OrgField | None:
        """The organization field a board field maps to: by reference first, then by display name."""
        return self.fields_by_ref.get(reference.lower()) or self.fields_by_name.get(name.lower())

    def done_states(self) -> set[str]:
        """Every state, of any listed type, in the Completed or Removed category."""
        return {s.name for t in self.types.values() for s in t.states if s.category in DONE_CATEGORIES}

    def completed_states(self) -> set[str]:
        return {s.name for t in self.types.values() for s in t.states if s.category == "Completed"}

    def removed_states(self) -> set[str]:
        return {s.name for t in self.types.values() for s in t.states if s.category == "Removed"}

    def category(self, type_name: str, state: str) -> str | None:
        t = next((t for t in self.types.values() if t.name.lower() == type_name.lower()), None)
        found = t.state(state) if t else None
        return found.category if found else None

    def area_path(self, name: str) -> str:
        """The Area Path a work item holds for a child of the root area: `Project\\Name`."""
        return f"{self.project}\\{name}"

    def area_name(self, path: str) -> str | None:
        """The board's area option for an Area Path, None for the root (blank) or a path the board does not name."""
        prefix = f"{self.project}\\".lower()
        if not path.lower().startswith(prefix):
            return None
        rest = path[len(prefix) :]
        return rest.split("\\", 1)[0] or None


def read_process(client: AdoClient, project: str) -> tuple[str, Process]:
    info = client.get(["_apis", "projects", project])
    project_id = text(info, "id", "the project")
    properties = value_list(client.get(["_apis", "projects", project_id, "properties"]), "project properties")
    type_id = next(
        (p.get("value") for p in properties if isinstance(p, dict) and p.get("name") == PROCESS_PROPERTY), ""
    )
    if not isinstance(type_id, str) or not type_id:
        raise MalformedDataError("the project's properties do not name its process")
    process = client.get(["_apis", "work", "processes", type_id])
    customization = text(process, "customizationType", "the process")
    return project_id, Process(type_id, text(process, "name", "the process"), customization)


def read_type(client: AdoClient, project: str, name: str) -> WorkItemType:
    what = f"work item type {name}"
    body = client.get([project, "_apis", "wit", "workitemtypes", name])
    states = tuple(
        State(text(raw, "name", what), text(raw, "category", what), text(raw, "color", what, optional=True))
        for raw in entries(body, "states", what)
    )
    fields = frozenset(text(f, "referenceName", what).lower() for f in entries(body, "fields", what))
    return WorkItemType(text(body, "name", what), text(body, "referenceName", what), states, fields)


def read_nodes(client: AdoClient, project: str, group: str) -> dict[str, Node]:
    """The direct children of the root node, by name. `group` is Areas or Iterations."""
    body = client.get([project, "_apis", "wit", "classificationnodes", group], {"$depth": 1})
    what = f"a child of the {group.lower()} root"
    children: dict[str, Node] = {}
    for raw in entries(body, "children", what):
        attributes = raw.get("attributes") if isinstance(raw, dict) else None
        attributes = attributes if isinstance(attributes, dict) else {}
        node = Node(
            text(raw, "name", what),
            text(raw, "path", what),
            text(raw, "identifier", what),
            day(attributes.get("startDate"), what),
            day(attributes.get("finishDate"), what),
        )
        children[node.name] = node
    return children


def read_team_iterations(client: AdoClient, project: str, team: str) -> frozenset[str] | None:
    try:
        body = client.get([project, team, "_apis", "work", "teamsettings", "iterations"])
    except NotFoundError:
        return None
    return frozenset(text(i, "id", "a team iteration").lower() for i in value_list(body, "team iterations"))


def read_queries(client: AdoClient, project: str, folder: str) -> dict[str, SharedQuery] | None:
    try:
        body = client.get(
            [project, "_apis", "wit", "queries", SHARED_QUERIES, folder], {"$depth": 1, "$expand": "wiql"}
        )
    except NotFoundError:
        return None
    queries: dict[str, SharedQuery] = {}
    for raw in entries(body, "children", "a shared query"):
        if flag(raw, "isFolder"):
            continue
        q = SharedQuery(
            text(raw, "id", "a shared query"),
            text(raw, "name", "a shared query"),
            text(raw, "wiql", "a shared query", optional=True),
        )
        queries[q.name] = q
    return queries


def read_fields(client: AdoClient, live: AdoLive) -> None:
    for raw in value_list(client.get(["_apis", "wit", "fields"]), "fields"):
        picklist = raw.get("picklistId") if isinstance(raw, dict) else None
        f = OrgField(
            text(raw, "name", "a field"),
            text(raw, "referenceName", "a field"),
            text(raw, "type", "a field"),
            picklist if isinstance(picklist, str) and flag(raw, "isPicklist") else "",
        )
        live.fields_by_name[f.name.lower()] = f
        live.fields_by_ref[f.reference.lower()] = f


def read_picklist(client: AdoClient, list_id: str) -> tuple[str, ...]:
    body = client.get(["_apis", "work", "processes", "lists", list_id])
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise MalformedDataError("a picklist's items are not a list of text")
    return tuple(items)


def load_live(client: AdoClient, board: Board) -> AdoLive:
    """Everything bootstrap and audit compare, and the types, fields and nodes reconcile and sync use."""
    azure = azure_of(board)
    project = board.project.title
    project_id, process = read_process(client, project)
    live = AdoLive(project_id, project, process)
    for name in azure.work_item_types:
        try:
            live.types[name] = read_type(client, project, name)
        except NotFoundError:
            continue  # audit reports it; bootstrap cannot make a work item type the process does not have
    read_fields(client, live)
    for f in board.fields:
        if f.type != "single_select" or role_of(board, f.name) is not None:
            continue
        found = live.org_field(f.name, reference_of(board, f))
        if found is not None and found.picklist and found.picklist not in live.picklists:
            live.picklists[found.picklist] = read_picklist(client, found.picklist)
    live.areas = read_nodes(client, project, "Areas")
    live.iterations = read_nodes(client, project, "Iterations")
    live.team_iterations = read_team_iterations(client, project, azure.team)
    live.queries = read_queries(client, project, azure.queries_folder)
    return live
