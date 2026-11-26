# SPDX-License-Identifier: Apache-2.0
"""The Azure DevOps part of board.yaml: `project.platform: azure-devops` and the `azure_devops` block.

How a board file maps onto Azure Boards (docs/azure-devops.md has the full table):

* `project.owner` is the organization (dev.azure.com/<owner>), `project.title` the project's name.
* `rules.status_field` is the work item State. Its options are states of every listed work item type.
* `rules.area_field` is the Area Path. Its options are area nodes directly under the project's root area.
* The one `iteration` field is the Iteration Path: iterations under the project's root, assigned to the team.
* Every other field is a custom field on the project's inherited process: `single_select` a picklist, `date` a
  date-time, `text` a string, `number` a double. Its reference name is `Custom.<Name>` unless `field_refs` says.
* A view is a shared query in `queries_folder`; its `filter` is a WIQL condition.

Names that reach a URL path or a WIQL string are checked here against strict allow-lists, so nothing in the board file
can change which resource a request names or what a query selects beyond its own condition.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from safo.errors import ConfigError

if TYPE_CHECKING:
    from safo.schema import Board, Field, Project, Reader, Repository

ORGANIZATION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?")
# A project, team, repository or work item type name: letters, digits and a little punctuation, no leading _ or .
NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._()&-]{0,62}[A-Za-z0-9)_-])?")
# An area or iteration node, or a state: no character that could form a path, a WIQL quote or a URL part
NODE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._()&-]{0,126}[A-Za-z0-9)_-])?")
# A custom field's display name; Azure DevOps refuses most punctuation in one
FIELD_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,126}[A-Za-z0-9_])?")
REFERENCE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:\.[A-Za-z][A-Za-z0-9_]{0,63}){1,3}")
FOLDER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,62}[A-Za-z0-9_-])?")
CATEGORIES = ("Proposed", "InProgress", "Resolved", "Completed", "Removed")
DONE_CATEGORIES = frozenset({"Completed", "Removed"})
MAX_TYPES = 16
MAX_FIELD_REFS = 100
MAX_AREA_RULES = 100
MAX_CLOSED_DAYS = 3650
DEFAULT_CLOSED_DAYS = 30

# The system fields a role of board.yaml maps onto
STATE = "System.State"
AREA_PATH = "System.AreaPath"
ITERATION_PATH = "System.IterationPath"
AZURE_KEYS = frozenset(
    {
        "team",
        "work_item_types",
        "default_area",
        "area_rules",
        "queries_folder",
        "status_page",
        "process",
        "state_categories",
        "field_refs",
        "closed_days",
    }
)


@dataclass(frozen=True)
class AzureDevOps:
    team: str
    work_item_types: tuple[str, ...]
    default_area: str | None = None
    area_rules: tuple[tuple[str, str], ...] = ()  # (match, area), first match wins
    queries_folder: str = "SAFO"
    status_page: str = "SAFO Status"
    process: str = ""  # the inherited process the project must use; empty: whatever it uses
    state_categories: tuple[tuple[str, str], ...] = ()  # state -> category, overriding the defaults
    field_refs: tuple[tuple[str, str], ...] = ()  # field name -> reference name, overriding Custom.<Name>
    closed_days: int = DEFAULT_CLOSED_DAYS  # reconcile reads items completed within this many days


def azure_of(board: Board) -> AzureDevOps:
    if board.azure is None:
        raise ConfigError("this board is not an Azure DevOps board (project.platform is not azure-devops)")
    return board.azure


def reference_for(azure: AzureDevOps, name: str) -> str:
    """The reference name of a custom field: the explicit one from `field_refs`, else Custom.<PascalCaseName>."""
    explicit = dict(azure.field_refs).get(name)
    if explicit:
        return explicit
    return "Custom." + "".join(word[:1].upper() + word[1:] for word in re.split(r"[ _-]+", name) if word)


def role_of(board: Board, field_name: str) -> str | None:
    """`state`, `area` or `iteration` for a field a system field stands for, None for a custom field."""
    if field_name == board.rules.status_field:
        return "state"
    if field_name == board.rules.area_field:
        return "area"
    f = board.field_named(field_name)
    if f is not None and f.type == "iteration":
        return "iteration"
    return None


def reference_of(board: Board, f: Field) -> str:
    role = role_of(board, f.name)
    if role == "state":
        return STATE
    if role == "area":
        return AREA_PATH
    if role == "iteration":
        return ITERATION_PATH
    return reference_for(azure_of(board), f.name)


def category_for(board: Board, state: str) -> str:
    """The state category a missing state is created in: `state_categories`, else the done states Completed, the
    opened and reopened states Proposed, anything else InProgress."""
    explicit = dict(azure_of(board).state_categories).get(state)
    if explicit:
        return explicit
    s = board.rules.status
    if state in (s.closed, s.merged):
        return "Completed"
    if state in (s.opened, s.reopened):
        return "Proposed"
    return "InProgress"


def area_for(board: Board, title: str, tags: tuple[str, ...]) -> str | None:
    """First matching area rule (case-insensitive substring of the title or a tag), else the default area."""
    azure = azure_of(board)
    text = f"{title} {' '.join(tags)}".lower()
    for match, area in azure.area_rules:
        if match.lower() in text:
            return area
    return azure.default_area


def parse_project(r: Reader, raw: object) -> Project:
    from safo.schema import Project, rep

    p = r.mapping(raw, "project", {"platform", "owner", "owner_type", "title"}, {"platform", "owner", "title"})
    owner = r.string(p["owner"], "project.owner")
    if not ORGANIZATION.fullmatch(owner):
        raise r.fail("project.owner", f"{rep(owner)} is not an Azure DevOps organization name")
    if "owner_type" in p and p["owner_type"] != "organization":
        raise r.fail("project.owner_type", "an Azure DevOps board belongs to an organization")
    title = r.string(p["title"], "project.title")
    if not NAME.fullmatch(title):
        raise r.fail("project.title", f"{rep(title)} is not an Azure DevOps project name safo accepts")
    return Project(owner, "organization", None, title, "azure-devops")


def check_repository(r: Reader, repo: Repository, path: str) -> None:
    from safo.schema import rep

    if not NAME.fullmatch(repo.owner):
        raise r.fail(f"{path}.owner", f"{rep(repo.owner)} is not an Azure DevOps project name safo accepts")
    if not NAME.fullmatch(repo.name):
        raise r.fail(f"{path}.name", f"{rep(repo.name)} is not an Azure Repos repository name safo accepts")
    if repo.default_area is not None or repo.area_rules:
        raise r.fail(
            path,
            "a work item is not tied to a repository on Azure DevOps: put default_area and area_rules under "
            "azure_devops instead",
        )
    if repo.method is not None:
        raise r.fail(f"{path}.method", "an Azure DevOps board has one process method, agile")


def _name(r: Reader, value: object, path: str, pattern: re.Pattern[str], what: str) -> str:
    from safo.schema import rep

    text = r.string(value, path)
    if not pattern.fullmatch(text):
        raise r.fail(path, f"{rep(text)} is not {what} safo accepts")
    return text


def _pairs(r: Reader, raw: object, path: str, cap: int) -> list[tuple[str, str]]:
    if not isinstance(raw, Mapping):
        raise r.fail(path, f"expected a mapping, got {type(raw).__name__}")
    if len(raw) > cap:
        raise r.fail(path, f"at most {cap} entries")
    m = r.mapping(raw, path, {k for k in raw if isinstance(k, str)}, bounded=True)
    return [(k, r.string(v, f"{path}.{k}")) for k, v in m.items()]


def attach(r: Reader, board: Board, raw: object) -> Board:
    """The board with its `azure_devops` block parsed and attached."""
    from safo.schema import rep

    if raw is None:
        raise r.fail("azure_devops", "required on a board with project.platform: azure-devops")
    m = r.mapping(raw, "azure_devops", set(AZURE_KEYS), {"work_item_types"})
    team = f"{board.project.title} Team"
    if "team" in m:
        team = _name(r, m["team"], "azure_devops.team", NAME, "a team name")
    raw_types = r.seq(m["work_item_types"], "azure_devops.work_item_types")
    if not raw_types or len(raw_types) > MAX_TYPES:
        raise r.fail("azure_devops.work_item_types", f"list between 1 and {MAX_TYPES} work item types")
    types: list[str] = []
    for i, value in enumerate(raw_types):
        path = f"azure_devops.work_item_types[{i}]"
        name = _name(r, value, path, NAME, "a work item type name")
        if name.lower() in {t.lower() for t in types}:
            raise r.fail(path, f"{rep(name)} is listed twice")
        types.append(name)
    default_area = None
    if m.get("default_area") is not None:
        default_area = r.string(m["default_area"], "azure_devops.default_area")
    raw_rules = r.seq(m.get("area_rules", []), "azure_devops.area_rules")
    if len(raw_rules) > MAX_AREA_RULES:
        raise r.fail("azure_devops.area_rules", f"at most {MAX_AREA_RULES} rules")
    area_rules = []
    for j, rule in enumerate(raw_rules):
        path = f"azure_devops.area_rules[{j}]"
        rm = r.mapping(rule, path, {"match", "area"}, {"match", "area"})
        area_rules.append((r.string(rm["match"], f"{path}.match"), r.string(rm["area"], f"{path}.area")))
    folder = _name(r, m.get("queries_folder", "SAFO"), "azure_devops.queries_folder", FOLDER, "a query folder name")
    page = r.string(m.get("status_page", "SAFO Status"), "azure_devops.status_page")
    if not all(FOLDER.fullmatch(part) for part in page.split("/")):
        raise r.fail("azure_devops.status_page", f"{rep(page)} is not a wiki page path safo accepts (A/B/C)")
    process = _name(r, m["process"], "azure_devops.process", NAME, "a process name") if "process" in m else ""
    categories = _pairs(r, m.get("state_categories", {}), "azure_devops.state_categories", MAX_FIELD_REFS)
    for state, category in categories:
        if category not in CATEGORIES:
            listed = ", ".join(CATEGORIES)
            raise r.fail(f"azure_devops.state_categories.{rep(state)}", f"{rep(category)} is not one of {listed}")
    refs = _pairs(r, m.get("field_refs", {}), "azure_devops.field_refs", MAX_FIELD_REFS)
    for name, ref in refs:
        if not REFERENCE.fullmatch(ref):
            raise r.fail(f"azure_devops.field_refs.{rep(name)}", f"{rep(ref)} is not a field reference name")
    closed_days = r.integer(m.get("closed_days", DEFAULT_CLOSED_DAYS), "azure_devops.closed_days")
    if closed_days > MAX_CLOSED_DAYS:
        raise r.fail("azure_devops.closed_days", f"at most {MAX_CLOSED_DAYS}")
    azure = AzureDevOps(
        team,
        tuple(types),
        default_area,
        tuple(area_rules),
        folder,
        page,
        process,
        tuple(categories),
        tuple(refs),
        closed_days,
    )
    return replace(board, azure=azure)


def cross_check(r: Reader, board: Board) -> None:
    """What only an Azure DevOps board must hold, after the shared cross-checks passed."""
    from safo.schema import rep

    azure = azure_of(board)
    rules = board.rules
    if board.process.method != "agile":
        raise r.fail("process.method", "an Azure DevOps board supports the agile method only")
    status = board.field_named(rules.status_field)
    if status is None or status.type != "single_select":
        raise r.fail("rules.status_field", f"{rep(rules.status_field)} must be a single_select field in fields")
    for i, option in enumerate(status.options):
        if not NODE.fullmatch(option.name):
            raise r.fail(f"fields.{rep(status.name)}.options[{i}]", f"{rep(option.name)} is not a state name")
    area = board.field_named(rules.area_field)
    areas: set[str] = set()
    if area is not None:
        if area.type != "single_select":
            raise r.fail("rules.area_field", f"{rep(rules.area_field)} must be a single_select field")
        for i, option in enumerate(area.options):
            if not NODE.fullmatch(option.name):
                raise r.fail(f"fields.{rep(area.name)}.options[{i}]", f"{rep(option.name)} is not an area name")
        areas = {o.name for o in area.options}
    wanted = [("azure_devops.default_area", azure.default_area)] + [
        (f"azure_devops.area_rules[{j}].area", a) for j, (_, a) in enumerate(azure.area_rules)
    ]
    for path, name in wanted:
        if name is not None and name not in areas:
            raise r.fail(path, f"{rep(name)} is not an option of the area field {rep(rules.area_field)}")
    iterations = [f for f in board.fields if f.type == "iteration"]
    if len(iterations) > 1:
        raise r.fail("fields", "an Azure DevOps board has one iteration field, the Iteration Path")
    for f in iterations:
        if not NODE.fullmatch(f"{f.title_prefix or f.name} {f.count}"):
            raise r.fail(f"fields.{rep(f.name)}.title_prefix", "does not make iteration names safo accepts")
    refs: dict[str, str] = {}
    for f in board.fields:
        if role_of(board, f.name) is not None:
            continue
        if not FIELD_NAME.fullmatch(f.name):
            raise r.fail(f"fields.{rep(f.name)}", "not a field name Azure DevOps accepts (letters, digits, space, - _)")
        ref = reference_for(azure, f.name)
        if not REFERENCE.fullmatch(ref):
            raise r.fail(
                f"fields.{rep(f.name)}", f"its reference name {rep(ref)} is invalid; set azure_devops.field_refs"
            )
        if ref.lower() in refs:
            raise r.fail(f"fields.{rep(f.name)}", f"has the same reference name as {rep(refs[ref.lower()])}: {ref}")
        refs[ref.lower()] = f.name
    names = {f.name for f in board.fields}
    for name, _ in azure.field_refs:
        if name not in names:
            raise r.fail(f"azure_devops.field_refs.{rep(name)}", "names a field that is not in fields")
        if role_of(board, name) is not None:
            raise r.fail(f"azure_devops.field_refs.{rep(name)}", "names the state, area or iteration field")
    states = {o.name for o in status.options}
    for state, _ in azure.state_categories:
        if state not in states:
            raise r.fail(f"azure_devops.state_categories.{rep(state)}", f"not an option of {rep(rules.status_field)}")
    for state in (rules.status.closed, rules.status.merged):
        if category_for(board, state) != "Completed":
            raise r.fail("azure_devops.state_categories", f"the done state {rep(state)} must be Completed")
    for i, v in enumerate(board.views):
        if not FOLDER.fullmatch(v.name):
            raise r.fail(f"views[{i}].name", f"{rep(v.name)} is not a query name safo accepts")
