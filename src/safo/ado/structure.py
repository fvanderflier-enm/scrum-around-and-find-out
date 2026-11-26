# SPDX-License-Identifier: Apache-2.0
"""What board.yaml asks of the Azure DevOps project that the project does not have: one `Gap` each.

`audit` prints every gap as drift; `bootstrap` creates the ones it may (`fixable`) and leaves the rest, said aloud.
Bootstrap only ever adds: a state, a field, a picklist, an area, an iteration, a team iteration, a query. It never
edits or removes what exists (a state's category, a picklist's items, an iteration's dates, a query's WIQL), for the
same reason the GitHub side never edits a field's options: a change there silently rewrites every work item that
holds the old value. States and fields can only be added to an inherited process; a system process is reported.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from safo.ado.config import azure_of, category_for, reference_of, role_of
from safo.ado.live import AdoLive
from safo.ado.workitems import wiql_list
from safo.schema import Board, Field

# The colour names of board.yaml, as the hex colours Azure DevOps gives a state
STATE_COLORS = {
    "GRAY": "b2b2b2",
    "BLUE": "007acc",
    "GREEN": "339933",
    "YELLOW": "fbbc3d",
    "ORANGE": "ff9d00",
    "RED": "cc293d",
    "PINK": "e87dd8",
    "PURPLE": "773b93",
}
# board.yaml field type -> the Azure DevOps field types that can hold it
FIELD_TYPES = {
    "single_select": ("string", "picklistString"),
    "text": ("string", "plainText", "picklistString"),
    "date": ("dateTime",),
    "number": ("double", "integer", "picklistDouble", "picklistInteger"),
}
CREATE_TYPES = {"single_select": "string", "text": "string", "date": "dateTime", "number": "double"}
VIEW_COLUMNS = (
    "[System.Id], [System.WorkItemType], [System.Title], [System.State], [System.AreaPath], [System.IterationPath]"
)
# The order bootstrap makes things in: what a later step refers to exists first
ORDER = ("state", "area", "iteration", "team_iteration", "field", "field_on_type", "folder", "query")


@dataclass(frozen=True)
class Gap:
    kind: str  # state, field, field_on_type, area, iteration, team_iteration, folder, query, or an unfixable kind
    text: str  # one line, as audit prints it after DRIFT
    fixable: bool  # bootstrap may make it
    data: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


@dataclass
class Findings:
    gaps: list[Gap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, kind: str, text: str, fixable: bool = False, **data: Any) -> None:
        self.gaps.append(Gap(kind, text, fixable, data))


def iteration_plan(f: Field) -> list[tuple[str, dt.date, dt.date]]:
    """(name, start, finish) of each iteration an iteration field asks for; the finish is the last day."""
    if f.start is None:
        return []
    prefix = f.title_prefix or f.name
    plan = []
    for i in range(f.count):
        start = f.start + dt.timedelta(days=f.duration_days * i)
        plan.append((f"{prefix} {i + 1}", start, start + dt.timedelta(days=f.duration_days - 1)))
    return plan


def view_wiql(board: Board, condition: str) -> str:
    types = wiql_list(azure_of(board).work_item_types)
    where = f"[System.TeamProject] = @project AND [System.WorkItemType] IN {types}"
    if condition.strip():
        where += f" AND ({condition.strip()})"
    return f"SELECT {VIEW_COLUMNS} FROM WorkItems WHERE {where} ORDER BY [System.Id]"  # noqa: S608 - WIQL


def _squash(wiql: str) -> str:
    return re.sub(r"\s+", " ", wiql).strip().lower()


def compare(board: Board, live: AdoLive) -> Findings:
    azure = azure_of(board)
    out = Findings()
    inherited = live.process.customization == "inherited"
    if azure.process and live.process.name != azure.process:
        out.add("process", f"the project uses the process {live.process.name!r}, board.yaml names {azure.process!r}")
    for name in azure.work_item_types:
        if name not in live.types:
            out.add("type", f"work item type {name!r} does not exist in the project's process (add it by hand)")
    status = board.field_named(board.rules.status_field)
    if status is not None:
        _compare_states(board, live, status, inherited, out)
    for f in board.fields:
        role = role_of(board, f.name)
        if role == "area":
            for option in f.options:
                if option.name not in live.areas:
                    out.add("area", f"area {option.name!r} is missing under {live.project}", True, name=option.name)
        elif role == "iteration":
            _compare_iterations(board, live, f, out)
        elif role is None:
            _compare_field(board, live, f, inherited, out)
    _compare_views(board, live, out)
    if not inherited and any(g.kind in ("state", "field_on_type") for g in out.gaps):
        out.notes.append(
            f"the project's process {live.process.name!r} is a {live.process.customization} process: states and "
            "fields can only be added to an inherited process (Organization settings > Process > Create inherited "
            "process, then change the project's process)"
        )
    out.gaps.sort(key=lambda g: ORDER.index(g.kind) if g.kind in ORDER else len(ORDER))
    return out


def _compare_states(board: Board, live: AdoLive, status: Field, inherited: bool, out: Findings) -> None:
    wanted = {o.name for o in status.options}
    for name, wit in live.types.items():
        for option in status.options:
            found = wit.state(option.name)
            category = category_for(board, option.name)
            if found is None:
                out.add(
                    "state",
                    f"work item type {name!r} has no state {option.name!r} ({category})",
                    inherited,
                    type=name,
                    state=option.name,
                    category=category,
                    color=STATE_COLORS.get(option.color, "b2b2b2"),
                )
            elif category == "Completed" and found.category != "Completed":
                out.add(
                    "state_category",
                    f"state {option.name!r} of {name!r} is in the {found.category} category, but it is a done "
                    "status: safo reads Done from the Completed category (changed only by hand)",
                )
        for state in wit.states:
            if state.name not in wanted and state.category != "Removed":
                out.notes.append(f"state {state.name!r} of {name!r} is not an option of {status.name!r}")


def _compare_iterations(board: Board, live: AdoLive, f: Field, out: Findings) -> None:
    azure = azure_of(board)
    if live.team_iterations is None:
        out.add("team", f"team {azure.team!r} does not exist in {live.project} (create it by hand)")
    for name, start, finish in iteration_plan(f):
        node = live.iterations.get(name)
        if node is None:
            out.add(
                "iteration",
                f"iteration {name!r} ({start} to {finish}) is missing",
                True,
                name=name,
                start=start,
                finish=finish,
            )
            if live.team_iterations is not None:
                out.add("team_iteration", f"iteration {name!r} is not selected by team {azure.team!r}", True, name=name)
            continue
        if (node.start, node.finish) != (start, finish):
            out.add(
                "iteration_dates",
                f"iteration {name!r} runs {node.start} to {node.finish}, board.yaml says {start} to {finish} "
                "(changed only by hand)",
            )
        if live.team_iterations is not None and node.identifier.lower() not in live.team_iterations:
            out.add("team_iteration", f"iteration {name!r} is not selected by team {azure.team!r}", True, name=name)


def _compare_field(board: Board, live: AdoLive, f: Field, inherited: bool, out: Findings) -> None:
    reference = reference_of(board, f)
    found = live.org_field(f.name, reference)
    options = [o.name for o in f.options]
    if found is None:
        kind = CREATE_TYPES[f.type] + (", picklist" if f.type == "single_select" else "")
        out.add(
            "field",
            f"field {f.name!r} ({reference}, {kind}) does not exist in the organization",
            True,
            name=f.name,
            reference=reference,
            type=CREATE_TYPES[f.type],
            options=options,
        )
    else:
        if found.type not in FIELD_TYPES[f.type]:
            out.add("field_type", f"field {f.name!r} is a {found.type} field, board.yaml says {f.type} (not changed)")
            return
        if f.type == "single_select":
            if not found.picklist:
                out.add("field_type", f"field {f.name!r} ({found.reference}) has no picklist (not changed)")
                return
            items = live.picklists.get(found.picklist, ())
            missing = [o for o in options if o not in items]
            if missing:
                out.add(
                    "picklist",
                    f"the picklist of {f.name!r} lacks {', '.join(repr(m) for m in missing)} "
                    "(bootstrap never edits an existing picklist)",
                )
        reference = found.reference
    for name, wit in live.types.items():
        if found is None or not wit.has_field(reference):
            out.add(
                "field_on_type",
                f"work item type {name!r} does not have the field {f.name!r} ({reference})",
                inherited,
                type=name,
                name=f.name,
                reference=reference,
            )


def _compare_views(board: Board, live: AdoLive, out: Findings) -> None:
    azure = azure_of(board)
    if not board.views:
        return
    if live.queries is None:
        out.add("folder", f"the shared query folder {azure.queries_folder!r} is missing", True)
    queries = live.queries or {}
    for v in board.views:
        expected = view_wiql(board, v.filter)
        found = queries.get(v.name)
        if found is None:
            out.add("query", f"shared query {v.name!r} is missing", True, name=v.name, wiql=expected)
        elif _squash(found.wiql) != _squash(expected):
            out.notes.append(f"shared query {v.name!r} has another WIQL than board.yaml gives; it is left alone")
