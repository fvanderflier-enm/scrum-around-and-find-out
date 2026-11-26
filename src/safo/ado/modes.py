# SPDX-License-Identifier: Apache-2.0
"""The board modes on an Azure DevOps board: bootstrap, audit, reconcile, sync, status and agents-status.

The command line builds an `AdoContext` instead of a GitHub `Context` when board.yaml says
`project.platform: azure-devops`, and calls `run` here. Each runner keeps its GitHub twin's contract: the same option
names, the same line prefixes (DRIFT, UNKNOWN, NOTE, HUMAN, CREATED, UPDATED ...) and the same exit codes, 0 clean or
applied, 1 drift or something left for a person, 2 the run cannot tell. After an unknown outcome nothing more is
written: the run re-reads what it can, says what it saw and stops.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from safo import output
from safo.ado.client import AdoClient, value_list
from safo.ado.config import azure_of
from safo.ado.live import AdoLive, load_live
from safo.ado.policy import (
    actual_reference,
    anomalies,
    desired_changes,
    gate_findings,
    hidden_pulls,
    is_completed,
    is_open,
    unthreatened,
    untracked_items,
)
from safo.ado.process import ProcessWriter
from safo.ado.structure import compare
from safo.ado.workitems import PullRequest, WorkItem, list_items, list_pulls, patch, read_item, read_pull
from safo.bounded import NotRegularFileError, read_bounded
from safo.coverage import not_configured
from safo.errors import (
    EXIT_DRIFT,
    EXIT_OK,
    EXIT_UNKNOWN,
    ApiError,
    ConfigError,
    MalformedDataError,
    NotFoundError,
    RateLimitedError,
    UnknownOutcomeError,
)
from safo.schema import Board, Repository
from safo.statusgroups import Row, group_rows, headline_acceptable, render_body, template_headline

MAX_EVENT_BYTES = 1024 * 1024
MAX_PAGE_BYTES = 65_536
SHOWN = 20


@dataclass
class AdoContext:
    board: Board
    client: AdoClient
    out: TextIO
    today: dt.date
    env: Mapping[str, str] = field(repr=False)

    def say(self, line: str = "") -> None:
        """One log line, escaped: nothing printed can act as a workflow command."""
        output.write(self.out, line)


Runner = Callable[[AdoContext, argparse.Namespace], int]


def run(mode: str, ctx: AdoContext, args: argparse.Namespace) -> int:
    runners: dict[str, Runner] = {
        "audit": audit,
        "bootstrap": bootstrap,
        "reconcile": reconcile,
        "sync": sync,
        "status": status,
        "agents-status": agents_status,
    }
    if mode not in runners:
        raise ConfigError(f"{mode} does not support an Azure DevOps board")
    try:
        return runners[mode](ctx, args)
    except RateLimitedError:
        ctx.say("rate limited before the next write was sent; try again later")
        return EXIT_DRIFT


def _label(ctx: AdoContext) -> str:
    return f"{ctx.board.project.owner}/{ctx.board.project.title}"


def _not_attempted(names: Sequence[str]) -> str:
    more = f" and {len(names) - SHOWN} more" if len(names) > SHOWN else ""
    return f"NOT ATTEMPTED {', '.join(names[:SHOWN])}{more}"


# -- audit ------------------------------------------------------------------------------------------------------------


def audit(ctx: AdoContext, args: argparse.Namespace) -> int:
    del args
    board = ctx.board
    try:
        live = load_live(ctx.client, board)
    except (NotFoundError, MalformedDataError) as err:
        ctx.say(f"UNKNOWN {err}")
        return EXIT_UNKNOWN
    found = compare(board, live)
    drift = [g.text for g in found.gaps]
    notes = list(found.notes)
    unknown: list[str] = []
    items, missing = list_items(ctx.client, board, live, azure_of(board).closed_days)
    if missing:
        unknown.append(f"{missing} work item(s) the query listed could not be read; the audit is incomplete")
    pulls, unreachable = list_pulls(ctx.client, board, None)
    unknown += [f"repository {name} cannot be read with this token" for name in unreachable]
    for item in items:
        changes = desired_changes(board, live, item, ctx.today)
        if changes:
            drift.append(f"{item.ref}: " + "; ".join(c.text for c in changes))
        drift += anomalies(board, live, item)
    drift += hidden_pulls(board, pulls) + untracked_items(board, live, items) + unthreatened(board, live, items)
    gate_drift, gate_unknown, gate_notes = gate_findings(ctx.client, board)
    drift += gate_drift
    unknown += gate_unknown
    notes += gate_notes + not_configured(board)
    unknown = list(dict.fromkeys(unknown))  # an unreadable repository is said once
    ctx.say(
        f"audit {_label(ctx)} ({live.process.name}): {len(board.fields)} fields, {len(board.views)} queries, "
        f"{len(items)} work items read"
    )
    for line in drift:
        ctx.say(f"DRIFT   {line}")
    for line in unknown:
        ctx.say(f"UNKNOWN {line}")
    for line in notes:
        ctx.say(f"NOTE    {line}")
    if board.ui_only:
        ctx.say("UI-only settings (no API can read or set these; check by eye):")
        for line in board.ui_only:
            ctx.say(f"  - {line}")
    code = EXIT_UNKNOWN if unknown else EXIT_DRIFT if drift else EXIT_OK
    summary = "clean" if code == EXIT_OK else f"{len(drift)} drift, {len(unknown)} unknown"
    if not board.rules.gate_check:
        summary += " (branch gate NOT checked: rules.gate_check is empty)"
    ctx.say(summary)
    return code


# -- bootstrap -------------------------------------------------------------------------------------------------------


def bootstrap(ctx: AdoContext, args: argparse.Namespace) -> int:
    del args
    board = ctx.board
    dry = ctx.client.dry_run
    live = load_live(ctx.client, board)
    found = compare(board, live)
    writer = ProcessWriter(ctx.client, board, live)
    created: list[str] = []
    left = [g.text for g in found.gaps if not g.fixable]
    blocked: set[str] = set()  # the names of iterations that could not be made: their team selection is not tried
    for gap in (g for g in found.gaps if g.fixable):
        if gap.kind == "team_iteration" and gap.data["name"] in blocked:
            left.append(gap.text)
            continue
        if gap.kind == "query" and "folder" in blocked:
            left.append(gap.text)
            continue
        try:
            writer.make(gap)
        except UnknownOutcomeError as err:
            for line in created:
                ctx.say(f"CREATED {line}")
            ctx.say(f"UNKNOWN {gap.text}: {err}")
            ctx.say("stopped: nothing more was sent; run `safo audit` to see what landed, then bootstrap again")
            return EXIT_UNKNOWN
        except ApiError as err:
            left.append(f"{gap.text}: refused ({err})")
            blocked.add("folder" if gap.kind == "folder" else str(gap.data.get("name", "")))
            continue
        created.append(gap.text)
    for line in created:
        ctx.say(f"{'WOULD CREATE' if dry else 'CREATED'} {line}")
    for line in left:
        ctx.say(f"LEFT    {line}")
    for line in found.notes:
        ctx.say(f"NOTE    {line}")
    if not found.gaps:
        ctx.say("nothing to create: the project already has everything board.yaml names")
    if board.ui_only:
        ctx.say("UI-only settings (no API can set these; do them once by hand):")
        for line in board.ui_only:
            ctx.say(f"  [ ] {line}")
    return EXIT_DRIFT if left or (dry and created) else EXIT_OK


# -- reconcile and sync ------------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return self.applied + self.unknown + self.failed


def _apply(ctx: AdoContext, live: AdoLive, item: WorkItem, out: Outcome, *, reopened: bool = False) -> bool:
    """Write what one work item is owed. False once an outcome is unknown: the caller sends nothing more."""
    changes = desired_changes(ctx.board, live, item, ctx.today, reopened=reopened)
    if not changes:
        return True
    what = "; ".join(c.text for c in changes)
    try:
        patch(ctx.client, ctx.board, item, changes)
    except UnknownOutcomeError as err:
        out.unknown.append(f"UNKNOWN {item.ref}: {err}")
        try:
            now = read_item(ctx.client, ctx.board, live, item.id)
        except ApiError:
            now = None
        seen = "could not be re-read" if now is None else f"is now at revision {now.rev} (was {item.rev})"
        out.unknown.append(f"OBSERVED {item.ref} {seen}")
        return False
    except ApiError as err:  # a reasoned refusal, such as the revision test: nothing was written
        out.failed.append(f"FAILED {item.ref}: {what} ({err})")
        return True
    out.applied.append(f"{'WOULD UPDATE' if ctx.client.dry_run else 'UPDATED'} {item.ref}: {what}")
    return True


def reconcile(ctx: AdoContext, args: argparse.Namespace) -> int:
    del args
    board = ctx.board
    dry = ctx.client.dry_run
    live = load_live(ctx.client, board)
    items, missing = list_items(ctx.client, board, live, azure_of(board).closed_days)
    pulls, unreachable = list_pulls(ctx.client, board, None)
    notes = [line for item in items for line in anomalies(board, live, item)]
    out = Outcome()
    rest: list[str] = []
    for i, item in enumerate(items):
        if not _apply(ctx, live, item, out):
            rest = [it.ref for it in items[i + 1 :] if desired_changes(board, live, it, ctx.today)]
            break
    for line in out.lines():
        ctx.say(line)
    if rest:
        ctx.say(_not_attempted(rest))
    for name in unreachable:
        ctx.say(f"UNREACHABLE {name}: the token cannot read it")
    for line in notes:
        ctx.say(f"NOTE {line}")
    human = hidden_pulls(board, pulls) + untracked_items(board, live, items) + unthreatened(board, live, items)
    for line in human:
        ctx.say(f"HUMAN {line}")
    if missing:
        ctx.say(f"UNKNOWN {missing} work item(s) the query listed could not be read; cannot tell the board is current")
    if out.unknown or unreachable or missing:
        return EXIT_UNKNOWN
    if notes or human or out.failed or (dry and out.applied):
        return EXIT_DRIFT
    if not out.applied:
        ctx.say("nothing to do: the board is current")
    return EXIT_OK


@dataclass(frozen=True)
class Pointer:
    kind: str  # workitem | pr
    number: int
    repo: Repository | None = None


def pointer_from_event(payload: Any) -> Pointer | None:
    """What a service hook event names, or None for an event that does not change a card. The payload is only a
    pointer: everything decided is read again."""
    if not isinstance(payload, dict):
        raise ConfigError("the event payload is not a JSON object")
    event = payload.get("eventType")
    resource = payload.get("resource")
    if not isinstance(event, str) or not isinstance(resource, dict):
        raise ConfigError("the event payload has no eventType and resource")
    if event.startswith("workitem."):
        raw = resource.get("workItemId") if event == "workitem.updated" else resource.get("id")
        if type(raw) is not int or raw < 1:
            raise ConfigError(f"the {output.line(event)[:60]} payload names no work item id")
        return Pointer("workitem", raw)
    if event.startswith("git.pullrequest."):
        number = resource.get("pullRequestId")
        repository = resource.get("repository")
        project = repository.get("project") if isinstance(repository, dict) else None
        name = repository.get("name") if isinstance(repository, dict) else None
        owner = project.get("name") if isinstance(project, dict) else None
        if type(number) is not int or number < 1 or not isinstance(name, str) or not isinstance(owner, str):
            raise ConfigError(f"the {output.line(event)[:60]} payload names no pull request and repository")
        return Pointer("pr", number, Repository(owner, name))
    return None


def _read_event(path: str) -> Any:
    try:
        raw = read_bounded(Path(path), MAX_EVENT_BYTES)
    except (OSError, NotRegularFileError) as err:
        raise ConfigError(f"cannot read the event payload: {type(err).__name__}") from None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ConfigError("the event payload is not JSON") from None


def sync(ctx: AdoContext, args: argparse.Namespace) -> int:
    board = ctx.board
    pointer: Pointer | None
    if getattr(args, "work_item", 0):
        pointer = Pointer("workitem", args.work_item)
    else:
        path = args.event_path or ctx.env.get("SAFO_EVENT_PATH", "")
        if not path:
            raise ConfigError("sync needs --work-item N, or a service hook payload in --event-path or $SAFO_EVENT_PATH")
        pointer = pointer_from_event(_read_event(path))
    if pointer is None:
        ctx.say("the event does not change a card; nothing to do")
        return EXIT_OK
    if pointer.kind == "pr":
        return _sync_pull(ctx, pointer)
    live = load_live(ctx.client, board)
    item = read_item(ctx.client, board, live, pointer.number)
    if item is None:
        ctx.say(f"VANISHED AB#{pointer.number} (confirmed by read)")
        return EXIT_UNKNOWN
    if item.type.lower() not in {t.lower() for t in azure_of(board).work_item_types}:
        ctx.say(f"NOTE {item.ref} is a {output.line(item.type)}, not a type board.yaml lists; nothing was written")
        return EXIT_OK
    out = Outcome()
    _apply(ctx, live, item, out, reopened=True)
    for line in out.lines():
        ctx.say(line)
    if out.unknown:
        return EXIT_UNKNOWN
    cleared = any("cleared" in line for line in out.applied)
    notes = [n for n in anomalies(board, live, item) if not (cleared and "sync clears it" in n)]
    human = untracked_items(board, live, [item]) + unthreatened(board, live, [item])
    for line in notes:
        ctx.say(f"NOTE {line}")
    for line in human:
        ctx.say(f"HUMAN {line}")
    if not (out.applied or notes or human or out.failed):
        ctx.say(f"{item.ref}: already in line")
    return EXIT_DRIFT if notes or human or out.failed or (ctx.client.dry_run and out.applied) else EXIT_OK


def _sync_pull(ctx: AdoContext, pointer: Pointer) -> int:
    board = ctx.board
    named = pointer.repo
    repo = None
    if named is not None:
        repo = next((r for r in board.repositories if r.full_name.lower() == named.full_name.lower()), None)
    if repo is None:
        shown = output.line(named.full_name)[:120] if named else "an unnamed repository"
        ctx.say(f"NOTE repository {shown} is not listed in board.yaml; nothing was read (add it under repositories)")
        return EXIT_DRIFT
    pull = read_pull(ctx.client, repo, pointer.number)
    if pull is None:
        ctx.say(f"VANISHED {repo.full_name}!{pointer.number} (confirmed by read)")
        return EXIT_UNKNOWN
    human = hidden_pulls(board, {repo.full_name.lower(): [pull]})
    for line in human:
        ctx.say(f"HUMAN {line}")
    if not human:
        ctx.say(f"{pull.label}: linked work items {_linked(pull)}")
    return EXIT_DRIFT if human else EXIT_OK


def _linked(pull: PullRequest) -> str:
    return ", ".join(f"AB#{n}" for n in pull.work_items) or "none (not required here)"


# -- status ---------------------------------------------------------------------------------------------------------


def status_rows(ctx: AdoContext, live: AdoLive, since: dt.date) -> tuple[list[Row], int]:
    """Every open work item, and every one completed since `since`, as status rows (`AB#n`)."""
    board = ctx.board
    days = max(0, (ctx.today - since).days) + 1
    items, missing = list_items(ctx.client, board, live, days)
    agent_ref = actual_reference(board, live, board.agents.field)
    rows = []
    for item in items:
        agent = item.value(agent_ref) if agent_ref else None
        when = (item.closed or item.changed) if is_completed(live, item) else None
        rows.append(Row("AB", item.id, item.title, item.state, agent if isinstance(agent, str) else "", when))
    return rows, missing


def _status_body(ctx: AdoContext, args: argparse.Namespace, live: AdoLive) -> tuple[str, int]:
    from safo.headline import local_headline
    from safo.modes.status import DEFAULT_SINCE_DAYS, _date_option

    since = _date_option(args.since, "--since") if args.since else ctx.today - dt.timedelta(days=DEFAULT_SINCE_DAYS)
    rows, missing = status_rows(ctx, live, since)
    groups = group_rows(rows, ctx.board, since, args.human_agent)
    headline = template_headline(groups)
    if not args.no_local:
        line = local_headline(ctx.env, args, groups)
        if line is not None and headline_acceptable(line, groups):
            headline += " " + line
    notes = [f"{missing} work item(s) could not be read with this token and are not counted above."] if missing else []
    return render_body(headline, groups, args.limit, notes), missing


def _wiki_id(ctx: AdoContext) -> str:
    project = ctx.board.project.title
    for wiki in value_list(ctx.client.get([project, "_apis", "wiki", "wikis"]), "wikis"):
        if isinstance(wiki, dict) and wiki.get("type") == "projectWiki" and isinstance(wiki.get("id"), str):
            return str(wiki["id"])
    raise ConfigError(f"{project} has no project wiki to post to: create it once (Overview > Wiki), or use --print")


def _page_exists(ctx: AdoContext, wiki: str, path: str) -> bool:
    try:
        ctx.client.get([ctx.board.project.title, "_apis", "wiki", "wikis", wiki, "pages"], {"path": path})
    except NotFoundError:
        return False
    return True


def _put_page(ctx: AdoContext, wiki: str, path: str, content: str) -> None:
    ctx.client.write(
        "PUT", [ctx.board.project.title, "_apis", "wiki", "wikis", wiki, "pages"], {"content": content}, {"path": path}
    )


def post_page(ctx: AdoContext, body: str, args: argparse.Namespace) -> int:
    """Post the update as a new wiki page `<status_page>/<today>`; an existing page is never overwritten."""
    azure = azure_of(ctx.board)
    header = [f"State: {args.state}"]
    if args.start_date:
        header.append(f"Start: {args.start_date}")
    if args.target_date:
        header.append(f"Target: {args.target_date}")
    content = " · ".join(header) + "\n\n" + body
    if len(content.encode()) > MAX_PAGE_BYTES:
        raise ConfigError(f"the update is longer than {MAX_PAGE_BYTES} bytes")
    wiki = _wiki_id(ctx)
    parts = azure.status_page.split("/")
    path = "/" + "/".join([*parts, ctx.today.isoformat()])
    if _page_exists(ctx, wiki, path):
        ctx.say(f"NOTE the wiki page {path} already exists; safo never overwrites an update: nothing was posted")
        return EXIT_DRIFT
    try:
        for depth in range(1, len(parts) + 1):
            parent = "/" + "/".join(parts[:depth])
            if not _page_exists(ctx, wiki, parent):
                _put_page(ctx, wiki, parent, "Status updates posted by safo.")
        _put_page(ctx, wiki, path, content)
    except UnknownOutcomeError as err:
        ctx.say(f"UNKNOWN {err}")
        return EXIT_UNKNOWN
    ctx.say(f"{'WOULD POST' if ctx.client.dry_run else 'POSTED'} the status update to the wiki page {path}")
    return EXIT_DRIFT if ctx.client.dry_run else EXIT_OK


def status(ctx: AdoContext, args: argparse.Namespace) -> int:
    from safo.modes.status import check_options, read_body

    check_options(args)
    live = load_live(ctx.client, ctx.board)
    if args.post:
        body, missing = _status_body(ctx, args, live)
    else:
        body, missing = read_body(args.body_file), 0
    if missing:
        ctx.say(f"WARNING {missing} work item(s) could not be read; the update says so and does not count them")
    if args.print_only:
        output.block(ctx.out, body.splitlines())
        return EXIT_DRIFT if missing else EXIT_OK
    code = post_page(ctx, body, args)
    return code if code != EXIT_OK or not missing else EXIT_DRIFT


# -- agents-status ----------------------------------------------------------------------------------------------------


def agents_status(ctx: AdoContext, args: argparse.Namespace) -> int:
    from safo.modes.agents_status import UNKNOWN, check_hours, local_sections

    board = ctx.board
    check_hours(args)
    live = load_live(ctx.client, board)
    items, _ = list_items(ctx.client, board, live, 0)
    agent_ref = actual_reference(board, live, board.agents.field)
    agent_field = board.field_named(board.agents.field)
    known = {o.name for o in agent_field.options} if agent_field else set()
    waiting: list[str] = []
    working: dict[str, list[str]] = defaultdict(list)
    unknown: set[str] = set()
    for item in items:
        if not is_open(live, item):
            continue
        raw: Any = item.value(agent_ref) if agent_ref else None
        named = raw if isinstance(raw, str) else ""
        shown = named or "unassigned"
        if named and named not in known and (item.state in board.agents.working or item.state in board.agents.waiting):
            unknown.add(named)
            shown = f"{named} {UNKNOWN}"
        if item.state in board.agents.waiting:
            waiting.append(f"  {shown:<12} {item.ref}  {item.title[:80]}")
        if item.state in board.agents.working:
            working[shown].append(f"  {shown:<12} {item.state:<12} {item.ref}  {item.title[:70]}")
    ctx.say(f"Waiting on you (State = {', '.join(board.agents.waiting)})")
    for line in sorted(waiting) or ["  nothing"]:
        ctx.say(line)
    ctx.say(f"\nBoard: {', '.join(board.agents.working)}, by agent ({_label(ctx)})")
    for agent in sorted(working):
        for line in sorted(working[agent]):
            ctx.say(line)
    if not working:
        ctx.say("  nothing")
    if unknown:
        ctx.say(
            f"\nUnknown agent values (not options of {board.agents.field} in board.yaml): {', '.join(sorted(unknown))}"
        )
    local_sections(ctx.say, ctx.env, board, args, known)
    return EXIT_OK
