# SPDX-License-Identifier: Apache-2.0
"""The decisions for an Azure DevOps board, as pure functions over what was read (and the gate's own reads).

* `desired_changes`: what one work item is owed, one change per field at most, in the GitHub side's priority order:
  the Done-on date a completed item lacks (or a reopened item still holds), then the Area Path when it is the root,
  then each `new_item_defaults` field that is blank. The State is never written: on Azure Boards it is the person's
  (or the process's) to move, and safo only reads it.
* `anomalies`: what is wrong and no rule may fix: a state the board does not know, an open item with a Done-on date.
* `hidden_pulls`, `untracked_items`, `unthreatened`: the no-hidden-work and threat-model findings.
* `gate_findings`: each listed repository's default branch must require the gate check through a blocking branch
  policy (a build validation or a status check of that name).
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from safo.ado.client import AdoClient
from safo.ado.config import AREA_PATH, area_for, reference_of, role_of
from safo.ado.live import AdoLive, text
from safo.ado.workitems import Change, PullRequest, WorkItem
from safo.coverage import tracked_names
from safo.errors import MalformedDataError, NotFoundError
from safo.schema import Board
from safo.threatmodel import verdict

DESCRIPTION = "System.Description"
BUILD_POLICY = "0609b952-1397-4640-95ec-e00a01b2c241"  # Build (build validation)
STATUS_POLICY = "cbdc66da-9728-4af8-aada-9a5a32e4a226"  # Status (a required status check)


def actual_reference(board: Board, live: AdoLive, field_name: str) -> str | None:
    """The reference name a board field is written through, None when the organization does not have it."""
    f = board.field_named(field_name)
    if f is None:
        return None
    if role_of(board, field_name) is not None:
        return reference_of(board, f)
    found = live.org_field(f.name, reference_of(board, f))
    return found.reference if found else None


def stamp(day: dt.date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def is_completed(live: AdoLive, item: WorkItem) -> bool:
    return live.category(item.type, item.state) == "Completed"


def is_open(live: AdoLive, item: WorkItem) -> bool:
    return live.category(item.type, item.state) not in ("Completed", "Removed")


def desired_changes(
    board: Board, live: AdoLive, item: WorkItem, today: dt.date, *, reopened: bool = False
) -> list[Change]:
    """What one work item needs. `reopened` (sync): an open item's stale Done-on date is cleared; reconcile only
    reports one."""
    rules = board.rules
    wit = next((t for t in live.types.values() if t.name.lower() == item.type.lower()), None)
    if wit is None:
        return []
    changes: list[Change] = []
    claimed: set[str] = set()

    def claim(change: Change) -> None:
        if change.reference.lower() not in claimed:
            claimed.add(change.reference.lower())
            changes.append(change)

    done_ref = actual_reference(board, live, rules.done_date_field) if rules.done_date_field else None
    if done_ref and wit.has_field(done_ref):
        held = item.value(done_ref)
        if is_completed(live, item) and held is None:
            when = item.closed or item.changed or today
            claim(Change(done_ref, stamp(when), f"{rules.done_date_field} = {when.isoformat()}"))
        elif reopened and is_open(live, item) and held is not None:
            claim(Change(done_ref, None, f"{rules.done_date_field} cleared"))
    if board.field_named(rules.area_field) is not None and live.area_name(item.area_path) is None:
        area = area_for(board, item.title, item.tags)
        if area and area in live.areas:
            claim(Change(AREA_PATH, live.area_path(area), f"{rules.area_field} = {area}"))
    roles = {rules.status_field, rules.area_field, rules.done_date_field}
    for name, option in rules.new_item_defaults:
        if name in roles or role_of(board, name) is not None:
            continue
        ref = actual_reference(board, live, name)
        if ref and wit.has_field(ref) and item.value(ref) in (None, ""):
            claim(Change(ref, option, f"{name} = {option}"))
    return changes


def anomalies(board: Board, live: AdoLive, item: WorkItem) -> list[str]:
    rules = board.rules
    status = board.field_named(rules.status_field)
    out = []
    if status is not None and item.state not in {o.name for o in status.options}:
        out.append(f"{item.ref} is in the state {item.state!r}, which is not an option of {rules.status_field!r}")
    done_ref = actual_reference(board, live, rules.done_date_field) if rules.done_date_field else None
    if done_ref and is_open(live, item) and item.value(done_ref) is not None:
        out.append(f"{item.ref} is open but holds a {rules.done_date_field} date; sync clears it on reopen")
    return out


def _author_exempt(board: Board, pull: PullRequest) -> bool:
    exempt = {a.lower() for a in board.rules.tracking.exempt_authors}
    names = {pull.author.lower(), pull.author.split("@")[0].lower(), pull.author_name.lower()} - {""}
    return bool(exempt & names)


def hidden_pulls(board: Board, pulls: Mapping[str, Sequence[PullRequest]]) -> list[str]:
    """Active pull requests that link no work item, except in exempt repositories or by exempt authors."""
    exempt_repos = {r.lower() for r in board.rules.tracking.exempt_repos}
    out = []
    for key, rows in sorted(pulls.items()):
        if key in exempt_repos:
            continue
        for pull in rows:
            if pull.status == "active" and not pull.work_items and not _author_exempt(board, pull):
                out.append(f"pull request {pull.label} links no work item (link one, or write AB#n in it)")
    return out


def untracked_items(board: Board, live: AdoLive, items: Iterable[WorkItem]) -> list[str]:
    """Open work items that hold no value for a configured Kind, Epic or Effort field."""
    refs = [(label, actual_reference(board, live, name)) for label, name in tracked_names(board)]
    if not refs:
        return []
    out = []
    for item in items:
        if not is_open(live, item):
            continue
        lacking = [label for label, ref in refs if ref is None or item.value(ref) in (None, "")]
        if lacking:
            out.append(f"{item.ref} has no {', '.join(lacking)}")
    return out


_BREAKS = re.compile(r"<\s*(?:br|/p|/div|/li|/h[1-6])\s*/?\s*>", re.IGNORECASE)
_TAGS = re.compile(r"<[^<>]{0,2000}>")


def plain_description(value: Any) -> str:
    """The text of an HTML description, one line per paragraph, so a `threat-model:` line is still a line."""
    if not isinstance(value, str):
        return ""
    return html.unescape(_TAGS.sub("", _BREAKS.sub("\n", value[:65_536])))


def unthreatened(board: Board, live: AdoLive, items: Iterable[WorkItem]) -> list[str]:
    """Open work items tagged with the threat-model label whose description names no usable threat model."""
    label = board.rules.threat_model.label
    if not label:
        return []
    out = []
    for item in items:
        if label.lower() not in {t.lower() for t in item.tags} or not is_open(live, item):
            continue
        reason = verdict(plain_description(item.value(DESCRIPTION)))
        if reason:
            out.append(f"{item.ref} is tagged {label!r} but {reason}")
    return out


# -- the branch gate ----------------------------------------------------------------------------------------------


def _scoped(config: dict[str, Any], repo_id: str, ref: str) -> bool:
    settings = config.get("settings")
    scopes = settings.get("scope") if isinstance(settings, dict) else None
    if not isinstance(scopes, list):
        return False
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        repository = scope.get("repositoryId")
        if repository is not None and str(repository).lower() != repo_id.lower():
            continue
        name, kind = scope.get("refName"), scope.get("matchKind", "exact")
        if name is None or name == ref or (kind == "prefix" and isinstance(name, str) and ref.startswith(name)):
            return True
    return False


def _names_gate(config: dict[str, Any], check: str) -> bool:
    kind = config.get("type")
    settings = config.get("settings")
    if not isinstance(kind, dict) or not isinstance(settings, dict):
        return False
    if kind.get("id") == BUILD_POLICY:
        return settings.get("displayName") == check
    if kind.get("id") == STATUS_POLICY:
        name, genre = settings.get("statusName"), settings.get("statusGenre")
        return check in (name, f"{genre}/{name}")
    return False


def _blocks(config: Any, repo_id: str, branch: str, check: str) -> bool:
    return (
        isinstance(config, dict)
        and config.get("isEnabled") is True
        and config.get("isBlocking") is True
        and config.get("isDeleted") is not True
        and _scoped(config, repo_id, branch)
        and _names_gate(config, check)
    )


def gate_findings(client: AdoClient, board: Board) -> tuple[list[str], list[str], list[str]]:
    """(drift, unknown, notes) for the branch gate of every listed repository."""
    rules = board.rules
    if not rules.gate_check:
        return [], [], []
    exempt = {r.lower() for r in rules.gate_exempt_repos}
    drift: list[str] = []
    unknown: list[str] = []
    configs: dict[str, list[Any]] = {}
    for repo in board.repositories:
        if repo.full_name.lower() in exempt:
            continue
        try:
            info = client.get([repo.owner, "_apis", "git", "repositories", repo.name])
            repo_id = text(info, "id", f"repository {repo.full_name}")
            branch = text(info, "defaultBranch", f"repository {repo.full_name}", optional=True)
            if not branch:
                unknown.append(f"{repo.full_name} has no default branch, so its gate cannot be read")
                continue
            if repo.owner not in configs:
                configs[repo.owner] = list(client.values([repo.owner, "_apis", "policy", "configurations"]))
        except NotFoundError:
            unknown.append(f"repository {repo.full_name} cannot be read with this token")
            continue
        except MalformedDataError as err:
            unknown.append(f"{repo.full_name}: {err}")
            continue
        if not any(_blocks(c, repo_id, branch, rules.gate_check) for c in configs[repo.owner]):
            drift.append(
                f"{repo.full_name}: {branch} has no enabled, blocking build validation or status policy named "
                f"{rules.gate_check!r}"
            )
    notes = []
    if rules.gate_forbid_force_push:
        notes.append("force pushes on Azure Repos are a Git permission (Force push), not a policy: check them by hand")
    return drift, unknown, notes
