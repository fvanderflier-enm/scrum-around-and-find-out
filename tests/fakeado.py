# SPDX-License-Identifier: Apache-2.0
"""A fake Azure DevOps REST API on 127.0.0.1: enough of it for safo's Azure DevOps modes, held in memory.

Everything is keyed the way Azure DevOps keys it (an organization, a project, names in the URL path), and every answer
is framed like the real one: JSON with a Content-Length. `fail` queues one answer for the next request whose method and
path match, so a test can make a write time out, be throttled or be refused.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ORG = "acme"
PROJECT = "Platform"
Answer = tuple[int, Any, dict[str, str]]
Handler = Callable[[list[str], dict[str, str], Any], Answer]


@dataclass
class Repo:
    project: str
    name: str
    id: str
    default_branch: str = "refs/heads/main"
    pulls: list[dict[str, Any]] = field(default_factory=list)
    links: dict[int, list[int]] = field(default_factory=dict)


@dataclass
class Canned:
    method: str
    path: str  # a substring of the decoded path
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    apply: bool = False  # perform the request first, then answer with this (a write that landed but looked lost)


class FakeAdo:
    def __init__(self) -> None:
        self.today = dt.date(2026, 10, 7)
        self.project_id = "p-1"
        self.process = {"typeId": "proc-1", "name": "SAFO Agile", "customizationType": "inherited"}
        self.types: dict[str, dict[str, Any]] = {}
        self.process_types: dict[str, dict[str, Any]] = {}
        self.fields: dict[str, dict[str, Any]] = {}
        self.picklists: dict[str, list[str]] = {}
        self.areas: dict[str, dict[str, Any]] = {}
        self.iterations: dict[str, dict[str, Any]] = {}
        self.teams: dict[str, set[str]] = {"Platform Team": set()}
        self.queries: dict[str, dict[str, Any]] | None = None
        self.items: dict[int, dict[str, Any]] = {}
        self.repos: dict[str, Repo] = {}
        self.policies: list[dict[str, Any]] = []
        self.wiki: str | None = "wiki-1"
        self.pages: dict[str, str] = {}
        self.log: list[tuple[str, str]] = []
        self.canned: list[Canned] = []
        self.seen_auth: set[str] = set()
        self._ids = 100
        for ref, name, kind in (
            ("System.Id", "ID", "integer"),
            ("System.Rev", "Rev", "integer"),
            ("System.WorkItemType", "Work Item Type", "string"),
            ("System.Title", "Title", "string"),
            ("System.State", "State", "string"),
            ("System.AreaPath", "Area Path", "treePath"),
            ("System.IterationPath", "Iteration Path", "treePath"),
            ("System.Tags", "Tags", "plainText"),
            ("System.ChangedDate", "Changed Date", "dateTime"),
            ("System.Description", "Description", "html"),
            ("Microsoft.VSTS.Common.ClosedDate", "Closed Date", "dateTime"),
            ("Microsoft.VSTS.Common.Priority", "Priority", "integer"),
        ):
            self.fields[ref.lower()] = {"name": name, "referenceName": ref, "type": kind, "isPicklist": False}
        states = [
            ("New", "Proposed"),
            ("Active", "InProgress"),
            ("Resolved", "Resolved"),
            ("Closed", "Completed"),
            ("Removed", "Removed"),
        ]
        for name, ref in (
            ("User Story", "Microsoft.VSTS.WorkItemTypes.UserStory"),
            ("Bug", "Microsoft.VSTS.WorkItemTypes.Bug"),
        ):
            self.types[name] = {
                "name": name,
                "referenceName": ref,
                "states": [{"name": s, "category": c, "color": "b2b2b2"} for s, c in states],
                "fields": {r for r in self.fields if r != "microsoft.vsts.common.priority"},
            }
            self.process_types[name] = {"name": name, "referenceName": ref, "customization": "system"}

    def new_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}-{self._ids}"

    # -- setup helpers -----------------------------------------------------------------------------------------

    def add_item(self, number: int, kind: str = "User Story", state: str = "New", **fields: Any) -> dict[str, Any]:
        values = {
            "System.Id": number,
            "System.WorkItemType": kind,
            "System.Title": fields.pop("title", f"item {number}"),
            "System.State": state,
            "System.AreaPath": fields.pop("area", PROJECT),
            "System.IterationPath": PROJECT,
            "System.ChangedDate": fields.pop("changed", f"{self.today.isoformat()}T09:00:00Z"),
        }
        values.update(fields)
        self.items[number] = {"id": number, "rev": 1, "fields": values}
        return self.items[number]

    def add_repo(self, name: str, project: str = PROJECT) -> Repo:
        repo = Repo(project, name, self.new_id("repo"))
        self.repos[f"{project}/{name}".lower()] = repo
        return repo

    def add_pull(
        self,
        repo: Repo,
        number: int,
        *,
        links: list[int] | None = None,
        author: str = "dev@acme.test",
        status: str = "active",
        closed: str | None = None,
    ) -> None:
        repo.pulls.append(
            {
                "pullRequestId": number,
                "title": f"change {number}",
                "status": status,
                "isDraft": False,
                "createdBy": {"uniqueName": author, "displayName": author.split("@")[0]},
                "closedDate": closed,
            }
        )
        repo.links[number] = list(links or [])

    def add_gate(self, repo: Repo, name: str = "CI green", *, blocking: bool = True) -> None:
        self.policies.append(
            {
                "id": len(self.policies) + 1,
                "isEnabled": True,
                "isBlocking": blocking,
                "isDeleted": False,
                "type": {"id": "0609b952-1397-4640-95ec-e00a01b2c241", "displayName": "Build"},
                "settings": {
                    "displayName": name,
                    "scope": [{"repositoryId": repo.id, "refName": repo.default_branch, "matchKind": "exact"}],
                },
            }
        )

    def fail(
        self,
        method: str,
        path: str,
        status: int,
        body: Any = None,
        *,
        apply: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.canned.append(Canned(method, path, status, body, headers or {}, apply))

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.log if m != "GET" and not p.endswith(("/wiql", "/workitemsbatch"))]

    # -- the router ----------------------------------------------------------------------------------------------

    def handle(self, method: str, path: str, query: dict[str, str], body: Any) -> Answer:
        self.log.append((method, path))
        for canned in list(self.canned):
            if canned.method == method and canned.path in path:
                self.canned.remove(canned)
                if canned.apply:
                    self.route(method, path, query, body)
                return canned.status, canned.body, dict(canned.headers)
        return self.route(method, path, query, body)

    def route(self, method: str, path: str, query: dict[str, str], body: Any) -> Answer:
        parts = path.strip("/").split("/")
        if parts[0] != ORG:
            return 404, {"message": "no such organization"}, {}
        rest = parts[1:]
        handler = self._match(method, "/".join(rest))
        if handler is None:
            return 404, {"message": f"the fake has no route for {method} {'/'.join(rest)}"}, {}
        return handler(rest, query, body)

    def _match(self, method: str, joined: str) -> Handler | None:
        table: list[tuple[str, str, Handler]] = [
            ("GET", r"_apis/projects/[^/]+/properties", self.get_properties),
            ("GET", r"_apis/projects/[^/]+", self.get_project),
            ("GET", r"_apis/work/processes/lists/[^/]+", self.get_picklist),
            ("POST", r"_apis/work/processes/lists", self.post_picklist),
            ("GET", r"_apis/work/processes/[^/]+/workitemtypes", self.get_process_types),
            ("POST", r"_apis/work/processes/[^/]+/workitemtypes", self.post_process_type),
            ("POST", r"_apis/work/processes/[^/]+/workItemTypes/[^/]+/states", self.post_state),
            ("POST", r"_apis/work/processes/[^/]+/workItemTypes/[^/]+/fields", self.post_type_field),
            ("GET", r"_apis/work/processes/[^/]+", self.get_process),
            ("GET", r"_apis/wit/fields", self.get_fields),
            ("POST", r"_apis/wit/fields", self.post_field),
            ("GET", r"[^/]+/_apis/wit/workitemtypes/[^/]+", self.get_type),
            ("GET", r"[^/]+/_apis/wit/classificationnodes/(Areas|Iterations)", self.get_nodes),
            ("POST", r"[^/]+/_apis/wit/classificationnodes/(Areas|Iterations)", self.post_node),
            ("GET", r"[^/]+/[^/]+/_apis/work/teamsettings/iterations", self.get_team_iterations),
            ("POST", r"[^/]+/[^/]+/_apis/work/teamsettings/iterations", self.post_team_iteration),
            ("GET", r"[^/]+/_apis/wit/queries/Shared Queries/[^/]+", self.get_queries),
            ("POST", r"[^/]+/_apis/wit/queries/Shared Queries/[^/]+", self.post_query),
            ("POST", r"[^/]+/_apis/wit/queries/Shared Queries", self.post_folder),
            ("POST", r"[^/]+/_apis/wit/wiql", self.post_wiql),
            ("POST", r"[^/]+/_apis/wit/workitemsbatch", self.post_batch),
            ("PATCH", r"[^/]+/_apis/wit/workitems/[0-9]+", self.patch_item),
            ("GET", r"[^/]+/_apis/git/repositories/[^/]+/pullrequests", self.get_pulls),
            ("GET", r"[^/]+/_apis/git/repositories/[^/]+/pullrequests/[0-9]+", self.get_pull),
            ("GET", r"[^/]+/_apis/git/repositories/[^/]+/pullRequests/[0-9]+/workitems", self.get_links),
            ("GET", r"[^/]+/_apis/git/repositories/[^/]+", self.get_repo),
            ("GET", r"[^/]+/_apis/policy/configurations", self.get_policies),
            ("GET", r"[^/]+/_apis/wiki/wikis", self.get_wikis),
            ("GET", r"[^/]+/_apis/wiki/wikis/[^/]+/pages", self.get_page),
            ("PUT", r"[^/]+/_apis/wiki/wikis/[^/]+/pages", self.put_page),
        ]
        for m, pattern, handler in table:
            if m == method and re.fullmatch(pattern, joined):
                return handler
        return None

    # -- project and process -------------------------------------------------------------------------------------

    def get_project(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        if rest[2] != PROJECT:
            return 404, {"message": "no such project"}, {}
        return 200, {"id": self.project_id, "name": PROJECT}, {}

    def get_properties(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        return 200, {"value": [{"name": "System.ProcessTemplateType", "value": self.process["typeId"]}]}, {}

    def get_process(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        return 200, dict(self.process), {}

    def get_process_types(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        return 200, {"value": list(self.process_types.values())}, {}

    def post_process_type(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        name = body["name"]
        ref = f"SAFOAgile.{name.replace(' ', '')}"
        self.process_types[name] = {"name": name, "referenceName": ref, "customization": "inherited"}
        self.types[name]["referenceName"] = ref
        return 200, self.process_types[name], {}

    def _type_by_ref(self, ref: str) -> dict[str, Any] | None:
        return next((t for t in self.types.values() if t["referenceName"] == ref), None)

    def _customizable(self, ref: str) -> dict[str, Any] | None:
        wit = self._type_by_ref(ref)
        if wit is None or self.process_types[wit["name"]]["customization"] == "system":
            return None
        return wit

    def post_state(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        wit = self._customizable(rest[5])
        if wit is None:
            return 400, {"message": "cannot customize a system work item type"}, {}
        wit["states"].append({"name": body["name"], "category": body["stateCategory"], "color": body["color"]})
        return 200, {"id": self.new_id("state"), **body}, {}

    def post_type_field(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        wit = self._customizable(rest[5])
        if wit is None:
            return 400, {"message": "cannot customize a system work item type"}, {}
        if body["referenceName"].lower() not in self.fields:
            return 404, {"message": "no such field"}, {}
        wit["fields"].add(body["referenceName"].lower())
        return 200, {"referenceName": body["referenceName"]}, {}

    def get_fields(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        return 200, {"value": list(self.fields.values()), "count": len(self.fields)}, {}

    def post_field(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        if body["referenceName"].lower() in self.fields or any(
            f["name"].lower() == body["name"].lower() for f in self.fields.values()
        ):
            return 409, {"message": "a field with that name exists"}, {}
        entry = {k: body[k] for k in ("name", "referenceName", "type", "isPicklist")}
        if body.get("picklistId"):
            entry["picklistId"] = body["picklistId"]
        self.fields[body["referenceName"].lower()] = entry
        return 200, entry, {}

    def get_picklist(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        items = self.picklists.get(rest[4])
        if items is None:
            return 404, {"message": "no such list"}, {}
        return 200, {"id": rest[4], "items": items}, {}

    def post_picklist(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        list_id = self.new_id("list")
        self.picklists[list_id] = list(body["items"])
        return 200, {"id": list_id, "items": body["items"]}, {}

    def get_type(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        wit = self.types.get(rest[4])
        if wit is None:
            return 404, {"message": "no such work item type"}, {}
        fields = [{"referenceName": self.fields[r]["referenceName"]} for r in sorted(wit["fields"]) if r in self.fields]
        answer = {"name": wit["name"], "referenceName": wit["referenceName"], "states": wit["states"], "fields": fields}
        return 200, answer, {}

    # -- classification, teams, queries ----------------------------------------------------------------------------

    def _group(self, rest: list[str]) -> dict[str, dict[str, Any]]:
        return self.areas if rest[4] == "Areas" else self.iterations

    def get_nodes(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        single = "Area" if rest[4] == "Areas" else "Iteration"
        root = {"name": PROJECT, "path": f"\\{PROJECT}\\{single}", "children": list(self._group(rest).values())}
        return 200, root, {}

    def post_node(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        nodes = self._group(rest)
        if body["name"] in nodes:
            return 409, {"message": "exists"}, {}
        single = "Area" if rest[4] == "Areas" else "Iteration"
        node: dict[str, Any] = {
            "name": body["name"],
            "path": f"\\{PROJECT}\\{single}\\{body['name']}",
            "identifier": self.new_id("node"),
        }
        if body.get("attributes"):
            node["attributes"] = body["attributes"]
        nodes[body["name"]] = node
        return 201, node, {}

    def get_team_iterations(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        team = self.teams.get(rest[1])
        if team is None:
            return 404, {"message": "no such team"}, {}
        return 200, {"value": [{"id": i} for i in sorted(team)]}, {}

    def post_team_iteration(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        self.teams[rest[1]].add(body["id"])
        return 200, {"id": body["id"]}, {}

    def get_queries(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        if self.queries is None:
            return 404, {"message": "no such folder"}, {}
        return 200, {"name": rest[5], "isFolder": True, "children": list(self.queries.values())}, {}

    def post_folder(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        self.queries = {}
        return 201, {"id": self.new_id("folder"), "name": body["name"], "isFolder": True}, {}

    def post_query(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        if self.queries is None:
            return 404, {"message": "no such folder"}, {}
        entry = {"id": self.new_id("query"), "name": body["name"], "wiql": body["wiql"], "isFolder": False}
        self.queries[body["name"]] = entry
        return 201, entry, {}

    # -- work items ------------------------------------------------------------------------------------------------

    def post_wiql(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        wiql = body["query"]

        def listed(pattern: str) -> set[str]:
            found = re.search(pattern + r" \(([^)]*)\)", wiql)
            return set() if found is None else {v.strip().strip("'") for v in found.group(1).split(",")}

        types = listed(r"\[System\.WorkItemType\] IN")
        not_in = listed(r"\[System\.State\] NOT IN")
        recent = re.search(r"\[System\.State\] IN \(([^)]*)\) AND \[System\.ChangedDate\] >= @today - ([0-9]+)", wiql)
        recent_states = {v.strip().strip("'") for v in recent.group(1).split(",")} if recent else set()
        cutoff = self.today - dt.timedelta(days=int(recent.group(2))) if recent else None
        ids = []
        for number, item in sorted(self.items.items()):
            f = item["fields"]
            if f["System.WorkItemType"] not in types:
                continue
            state = f["System.State"]
            changed = dt.date.fromisoformat(f["System.ChangedDate"][:10])
            if state not in not_in or (state in recent_states and cutoff is not None and changed >= cutoff):
                ids.append(number)
        return 200, {"workItems": [{"id": i} for i in ids]}, {}

    def post_batch(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        wanted = {r.lower() for r in body["fields"]}
        for ref in wanted:
            if ref not in self.fields:
                return 400, {"message": f"TF51535: Cannot find field {ref}."}, {}
        rows: list[Any] = []
        for number in body["ids"]:
            item = self.items.get(number)
            if item is None:
                rows.append(None)
                continue
            fields = {k: v for k, v in item["fields"].items() if k.lower() in wanted}
            rows.append({"id": number, "rev": item["rev"], "fields": fields})
        return 200, {"value": rows, "count": len(rows)}, {}

    def patch_item(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        item = self.items.get(int(rest[4]))
        if item is None:
            return 404, {"message": "no such work item"}, {}
        fields = dict(item["fields"])
        for op in body:
            if op["op"] == "test":
                if op["path"] == "/rev" and op["value"] != item["rev"]:
                    return 412, {"message": "TF26071: This work item has been changed by someone else"}, {}
                continue
            ref = op["path"].removeprefix("/fields/")
            if op["op"] == "remove":
                fields.pop(ref, None)
            else:
                fields[ref] = op["value"]
        item["fields"] = fields
        item["rev"] += 1
        return 200, {"id": item["id"], "rev": item["rev"], "fields": fields}, {}

    # -- repositories ------------------------------------------------------------------------------------------------

    def _repo(self, rest: list[str]) -> Repo | None:
        return self.repos.get(f"{rest[0]}/{rest[4]}".lower())

    def get_repo(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        repo = self._repo(rest)
        if repo is None:
            return 404, {"message": "no such repository"}, {}
        return 200, {"id": repo.id, "name": repo.name, "defaultBranch": repo.default_branch}, {}

    def get_pulls(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        repo = self._repo(rest)
        if repo is None:
            return 404, {"message": "no such repository"}, {}
        status = q.get("searchCriteria.status", "active")
        rows = [p for p in repo.pulls if p["status"] == status]
        skip, top = int(q.get("$skip", 0)), int(q.get("$top", 100))
        return 200, {"value": rows[skip : skip + top]}, {}

    def get_pull(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        repo = self._repo(rest)
        found = next((p for p in repo.pulls if p["pullRequestId"] == int(rest[6])), None) if repo else None
        return (200, found, {}) if found else (404, {"message": "no such pull request"}, {})

    def get_links(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        repo = self._repo(rest)
        if repo is None:
            return 404, {"message": "no such repository"}, {}
        return 200, {"value": [{"id": str(i)} for i in repo.links.get(int(rest[6]), [])]}, {}

    def get_policies(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        return 200, {"value": self.policies}, {}

    # -- wiki --------------------------------------------------------------------------------------------------------

    def get_wikis(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        return 200, {"value": [{"id": self.wiki, "type": "projectWiki"}] if self.wiki else []}, {}

    def get_page(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        path = q["path"]
        if path not in self.pages:
            return 404, {"message": "no such page"}, {}
        return 200, {"path": path, "content": self.pages[path]}, {}

    def put_page(self, rest: list[str], q: dict[str, str], body: Any) -> Answer:
        path = q["path"]
        parent = path.rsplit("/", 1)[0]
        if parent and parent not in self.pages:
            return 400, {"message": "the parent page does not exist"}, {}
        if path in self.pages:
            return 412, {"message": "the page exists"}, {}
        self.pages[path] = body["content"]
        return 201, {"path": path}, {}


class _Handler(BaseHTTPRequestHandler):
    fake: FakeAdo

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        self.fake.seen_auth.add(self.headers.get("Authorization", ""))
        status, answer, headers = self.fake.handle(self.command, path, query, body)
        data = b"" if answer is None else json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", headers.pop("Content-Type", "application/json; charset=utf-8"))
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    do_GET = _serve
    do_POST = _serve
    do_PATCH = _serve
    do_PUT = _serve


@contextmanager
def serve(fake: FakeAdo) -> Iterator[str]:
    """The fake on a free loopback port; yields its base URL."""
    handler = type("Handler", (_Handler,), {"fake": fake})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
