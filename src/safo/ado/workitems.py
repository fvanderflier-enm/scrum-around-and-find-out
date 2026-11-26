# SPDX-License-Identifier: Apache-2.0
"""Work items and pull requests on Azure DevOps: list, read one, write one.

A work item is the card itself (there is no separate "add to board" step), so a listing is a WIQL query over the
project's listed work item types, and a read is the work item batch. A write is one JSON Patch whose first operation
tests the revision the decision was made from: if anyone changed the work item since, Azure DevOps refuses the whole
patch and nothing is written. A pull request is read with the work items linked to it, for the no-hidden-work rule.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from safo.ado.client import AdoClient, value_list
from safo.ado.config import azure_of, reference_of, role_of
from safo.ado.live import AdoLive, day, entries, text
from safo.errors import ApiError, MalformedDataError, NotFoundError, UnknownOutcomeError
from safo.schema import Board, Repository
from safo.values import whole_number

MAX_WORK_ITEMS = 19_999  # WIQL answers at most 20,000; one less tells "exactly that many" from "cut short"
BATCH = 200  # the work item batch reads at most 200 ids at a time
PULL_PAGE = 100
MAX_PULL_PAGES = 100
CLOSED_DATE = "Microsoft.VSTS.Common.ClosedDate"
DESCRIPTION = "System.Description"
SYSTEM_FIELDS = (
    "System.Id",
    "System.Rev",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AreaPath",
    "System.IterationPath",
    "System.Tags",
    "System.ChangedDate",
)


@dataclass(frozen=True)
class WorkItem:
    id: int
    rev: int
    type: str
    title: str
    state: str
    area_path: str
    iteration_path: str
    tags: tuple[str, ...]
    changed: dt.date | None
    closed: dt.date | None
    values: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)  # lower-case reference -> value

    @property
    def ref(self) -> str:
        return f"AB#{self.id}"

    def value(self, reference: str) -> Any:
        return self.values.get(reference.lower())


@dataclass(frozen=True)
class PullRequest:
    id: int
    repo: str  # project/name, as board.yaml lists it
    title: str
    status: str  # active | completed | abandoned
    draft: bool
    author: str  # the creator's unique name (an e-mail address or a service identity)
    author_name: str
    closed: dt.date | None
    work_items: tuple[int, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.repo}!{self.id}"


def wiql_string(value: str) -> str:
    """A WIQL string literal: single-quoted, a quote doubled."""
    return "'" + value.replace("'", "''") + "'"


def wiql_list(values: Iterable[str]) -> str:
    return "(" + ", ".join(wiql_string(v) for v in sorted(values)) + ")"


def items_wiql(board: Board, live: AdoLive, recent_days: int) -> str:
    """Every work item of the listed types that is not done, and those completed within `recent_days`.

    A removed work item is never read: Removed is not Done, and safo never brings one back.
    """
    types = wiql_list(azure_of(board).work_item_types)
    done = live.done_states()
    completed = live.completed_states()
    where = f"[System.TeamProject] = @project AND [System.WorkItemType] IN {types}"
    if not done:
        return f"SELECT [System.Id] FROM WorkItems WHERE {where} ORDER BY [System.Id]"  # noqa: S608 - quoted WIQL
    open_part = f"[System.State] NOT IN {wiql_list(done)}"
    if completed and recent_days > 0:
        recent = f"[System.State] IN {wiql_list(completed)} AND [System.ChangedDate] >= @today - {int(recent_days)}"
        open_part = f"({open_part} OR ({recent}))"
    return f"SELECT [System.Id] FROM WorkItems WHERE {where} AND {open_part} ORDER BY [System.Id]"  # noqa: S608


def query_ids(client: AdoClient, project: str, wiql: str) -> list[int]:
    body = client.query([project, "_apis", "wit", "wiql"], {"query": wiql}, {"$top": MAX_WORK_ITEMS + 1})
    rows = entries(body, "workItems", "the WIQL answer")
    if len(rows) > MAX_WORK_ITEMS:
        raise ApiError(f"the query matches more than {MAX_WORK_ITEMS} work items; safo does not read a partial board")
    ids = [whole_number(row.get("id") if isinstance(row, dict) else None, "a WIQL work item id") for row in rows]
    if len(set(ids)) != len(ids):
        raise MalformedDataError("the WIQL answer lists a work item twice")
    return ids


def wanted_fields(board: Board, live: AdoLive) -> list[str]:
    """The fields a read asks for: the system ones, the closed date, and every custom field of the board that exists."""
    refs = list(SYSTEM_FIELDS)
    if CLOSED_DATE.lower() in live.fields_by_ref:
        refs.append(CLOSED_DATE)
    if board.rules.threat_model.label:
        refs.append(DESCRIPTION)
    for f in board.fields:
        if role_of(board, f.name) is not None:
            continue
        found = live.org_field(f.name, reference_of(board, f))
        if found is not None and found.reference not in refs:
            refs.append(found.reference)
    return refs


def _tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise MalformedDataError("a work item's tags are not text")
    return tuple(t.strip() for t in value.split(";") if t.strip())


def work_item(raw: Any) -> WorkItem:
    what = "a work item"
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        raise MalformedDataError(f"{what} is not shaped as expected")
    fields: dict[str, Any] = raw["fields"]
    return WorkItem(
        whole_number(raw.get("id"), "a work item id"),
        whole_number(raw.get("rev"), "a work item revision"),
        text(fields, "System.WorkItemType", what),
        text(fields, "System.Title", what),
        text(fields, "System.State", what),
        text(fields, "System.AreaPath", what),
        text(fields, "System.IterationPath", what, optional=True),
        _tags(fields.get("System.Tags")),
        day(fields.get("System.ChangedDate"), what),
        day(fields.get(CLOSED_DATE), what),
        {k.lower(): v for k, v in fields.items()},
    )


def read_items(client: AdoClient, project: str, ids: Sequence[int], refs: Sequence[str]) -> tuple[list[WorkItem], int]:
    """The work items with these ids, and how many could not be read (deleted since, or not visible to the token)."""
    items: list[WorkItem] = []
    missing = 0
    for start in range(0, len(ids), BATCH):
        chunk = list(ids[start : start + BATCH])
        body = client.query(
            [project, "_apis", "wit", "workitemsbatch"], {"ids": chunk, "fields": list(refs), "errorPolicy": "omit"}
        )
        rows = value_list(body, "the work item batch")
        if len(rows) != len(chunk):
            raise MalformedDataError("the work item batch answered a different number of items than asked")
        for asked, row in zip(chunk, rows, strict=True):
            if row is None:
                missing += 1
                continue
            item = work_item(row)
            if item.id != asked:
                raise MalformedDataError("the work item batch answered the items out of order")
            items.append(item)
    return items, missing


def list_items(client: AdoClient, board: Board, live: AdoLive, recent_days: int) -> tuple[list[WorkItem], int]:
    project = board.project.title
    ids = query_ids(client, project, items_wiql(board, live, recent_days))
    return read_items(client, project, ids, wanted_fields(board, live))


def read_item(client: AdoClient, board: Board, live: AdoLive, number: int) -> WorkItem | None:
    """One work item, None when it does not exist (or the token cannot see it)."""
    items, _ = read_items(client, board.project.title, [number], wanted_fields(board, live))
    return items[0] if items else None


# -- writes ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    reference: str
    value: Any  # None removes the value
    text: str  # what the report says


def patch(client: AdoClient, board: Board, item: WorkItem, changes: Sequence[Change]) -> int:
    """Apply the changes in one patch that first tests the revision. The new revision (the old one on a dry run)."""
    ops: list[dict[str, Any]] = [{"op": "test", "path": "/rev", "value": item.rev}]
    for c in changes:
        if c.value is None:
            ops.append({"op": "remove", "path": f"/fields/{c.reference}"})
        else:
            ops.append({"op": "add", "path": f"/fields/{c.reference}", "value": c.value})
    answer = client.write(
        "PATCH",
        [board.project.title, "_apis", "wit", "workitems", str(item.id)],
        ops,
        content_type="application/json-patch+json",
        dry_result={"id": item.id, "rev": item.rev},
    )
    if client.dry_run:
        return item.rev
    rev = answer.get("rev") if isinstance(answer, dict) else None
    if not isinstance(answer, dict) or answer.get("id") != item.id or not isinstance(rev, int):
        raise UnknownOutcomeError(
            f"{item.ref}: the answer does not name the work item written, so it is not known what changed; "
            "re-read it before retrying, do not simply resend it"
        )
    return rev


# -- pull requests -------------------------------------------------------------------------------------------------


def _pull(raw: Any, repo: Repository) -> PullRequest:
    what = f"a pull request of {repo.full_name}"
    if not isinstance(raw, dict):
        raise MalformedDataError(f"{what} is not an object")
    author = raw.get("createdBy") if isinstance(raw.get("createdBy"), dict) else {}
    return PullRequest(
        whole_number(raw.get("pullRequestId"), f"{what}: the id"),
        repo.full_name,
        text(raw, "title", what),
        text(raw, "status", what),
        raw.get("isDraft") is True,
        text(author, "uniqueName", what, optional=True),
        text(author, "displayName", what, optional=True),
        day(raw.get("closedDate"), what),
    )


def _pulls(client: AdoClient, repo: Repository, params: dict[str, str | int]) -> list[PullRequest]:
    out: list[PullRequest] = []
    seen: set[int] = set()
    for page in range(MAX_PULL_PAGES):
        body = client.get(
            [repo.owner, "_apis", "git", "repositories", repo.name, "pullrequests"],
            {**params, "$top": PULL_PAGE, "$skip": page * PULL_PAGE},
        )
        rows = value_list(body, f"the pull requests of {repo.full_name}")
        for raw in rows:
            pull = _pull(raw, repo)
            if pull.id not in seen:  # a pull request that moved between pages is read once
                seen.add(pull.id)
                out.append(pull)
        if len(rows) < PULL_PAGE:
            return out
    raise ApiError(f"{repo.full_name} has more than {MAX_PULL_PAGES * PULL_PAGE} pull requests to read")


def linked_items(client: AdoClient, repo: Repository, number: int) -> tuple[int, ...]:
    body = client.get([repo.owner, "_apis", "git", "repositories", repo.name, "pullRequests", str(number), "workitems"])
    ids = []
    for row in value_list(body, f"the work items of {repo.full_name}!{number}"):
        raw = row.get("id") if isinstance(row, dict) else None
        if isinstance(raw, str) and raw.isdigit():
            raw = int(raw)
        ids.append(whole_number(raw, "a linked work item id"))
    return tuple(sorted(set(ids)))


def with_links(client: AdoClient, repo: Repository, pull: PullRequest) -> PullRequest:
    return PullRequest(
        pull.id,
        pull.repo,
        pull.title,
        pull.status,
        pull.draft,
        pull.author,
        pull.author_name,
        pull.closed,
        linked_items(client, repo, pull.id),
    )


def list_pulls(
    client: AdoClient, board: Board, closed_since: dt.date | None
) -> tuple[dict[str, list[PullRequest]], list[str]]:
    """The active pull requests of every listed repository (and those completed since `closed_since`), each with its
    linked work items; and the repositories that could not be read."""
    work: dict[str, list[PullRequest]] = {}
    unreachable: list[str] = []
    for repo in board.repositories:
        try:
            pulls = _pulls(client, repo, {"searchCriteria.status": "active"})
            if closed_since is not None:
                done = _pulls(
                    client,
                    repo,
                    {
                        "searchCriteria.status": "completed",
                        "searchCriteria.queryTimeRangeType": "closed",
                        "searchCriteria.minTime": f"{closed_since.isoformat()}T00:00:00Z",
                    },
                )
                pulls += [p for p in done if p.closed is not None and p.closed >= closed_since]
            work[repo.full_name.lower()] = [with_links(client, repo, p) for p in pulls]
        except NotFoundError:
            unreachable.append(repo.full_name)
    return work, unreachable


def read_pull(client: AdoClient, repo: Repository, number: int) -> PullRequest | None:
    try:
        body = client.get([repo.owner, "_apis", "git", "repositories", repo.name, "pullrequests", str(number)])
    except NotFoundError:
        return None
    return with_links(client, repo, _pull(body, repo))
