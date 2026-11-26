# SPDX-License-Identifier: Apache-2.0
"""bootstrap's writes on Azure DevOps: make one `Gap` that `structure.compare` found. Each one only adds.

Process changes (a state, a field on a work item type) go to the project's inherited process. A work item type the
inherited process still takes unchanged from its parent (`customization: system`) is first derived, which is what the
web UI does on the first change to it; the derived type keeps the name, so work items do not move.
"""

from __future__ import annotations

from typing import Any

from safo.ado.client import AdoClient, value_list
from safo.ado.config import azure_of
from safo.ado.live import SHARED_QUERIES, AdoLive, Node, text
from safo.ado.structure import Gap
from safo.errors import MalformedDataError
from safo.schema import Board


class ProcessWriter:
    def __init__(self, client: AdoClient, board: Board, live: AdoLive) -> None:
        self._client = client
        self._board = board
        self._live = live
        self._process_types: dict[str, dict[str, Any]] | None = None  # name -> the process's type entry

    # -- the process -------------------------------------------------------------------------------------------

    def _types(self) -> dict[str, dict[str, Any]]:
        if self._process_types is None:
            rows = value_list(
                self._client.get(["_apis", "work", "processes", self._live.process.id, "workitemtypes"]),
                "the process's work item types",
            )
            self._process_types = {text(r, "name", "a process work item type"): r for r in rows if isinstance(r, dict)}
        return self._process_types

    def _type_reference(self, name: str) -> str:
        """The reference of the process's own copy of a type, deriving it first when it is still the parent's."""
        types = self._types()
        found = types.get(name)
        if found is None:
            raise MalformedDataError(f"the process has no work item type {name!r}")
        reference = text(found, "referenceName", f"work item type {name}")
        if found.get("customization") != "system":
            return reference
        body = {
            "inheritsFrom": reference,
            "name": name,
            "color": found.get("color") or "009ccc",
            "icon": found.get("icon") or "icon_list",
            "isDisabled": False,
            "description": found.get("description") or "",
        }
        made = self._client.write(
            "POST",
            ["_apis", "work", "processes", self._live.process.id, "workitemtypes"],
            body,
            dry_result={"referenceName": reference},
        )
        derived = text(made, "referenceName", f"the derived work item type {name}")
        types[name] = {**found, "referenceName": derived, "customization": "inherited"}
        return derived

    def add_state(self, gap: Gap) -> None:
        wit = self._type_reference(gap.data["type"])
        self._client.write(
            "POST",
            ["_apis", "work", "processes", self._live.process.id, "workItemTypes", wit, "states"],
            {"name": gap.data["state"], "color": gap.data["color"], "stateCategory": gap.data["category"]},
        )

    def add_field(self, gap: Gap) -> None:
        """An organization field (with its picklist for a single select). Adding it to the types is its own gap."""
        picklist = None
        if gap.data["options"]:
            made = self._client.write(
                "POST",
                ["_apis", "work", "processes", "lists"],
                {
                    "name": f"picklist_{gap.data['reference'].replace('.', '_')}",
                    "type": "String",
                    "items": list(gap.data["options"]),
                    "isSuggested": False,
                },
                dry_result={"id": "DRY_RUN"},
            )
            picklist = text(made, "id", "the new picklist")
        body: dict[str, Any] = {
            "name": gap.data["name"],
            "referenceName": gap.data["reference"],
            "type": gap.data["type"],
            "usage": "workItem",
            "readOnly": False,
            "canSortBy": True,
            "isQueryable": True,
            "isPicklist": picklist is not None,
            "description": "Managed by safo from board.yaml",
        }
        if picklist is not None:
            body["picklistId"] = picklist
        self._client.write("POST", ["_apis", "wit", "fields"], body)

    def add_field_to_type(self, gap: Gap) -> None:
        wit = self._type_reference(gap.data["type"])
        self._client.write(
            "POST",
            ["_apis", "work", "processes", self._live.process.id, "workItemTypes", wit, "fields"],
            {"referenceName": gap.data["reference"], "required": False, "readOnly": False, "allowGroups": False},
        )

    # -- the project -----------------------------------------------------------------------------------------------

    def add_area(self, gap: Gap) -> None:
        self._client.write(
            "POST", [self._live.project, "_apis", "wit", "classificationnodes", "Areas"], {"name": gap.data["name"]}
        )

    def add_iteration(self, gap: Gap) -> None:
        made = self._client.write(
            "POST",
            [self._live.project, "_apis", "wit", "classificationnodes", "Iterations"],
            {
                "name": gap.data["name"],
                "attributes": {
                    "startDate": f"{gap.data['start'].isoformat()}T00:00:00Z",
                    "finishDate": f"{gap.data['finish'].isoformat()}T00:00:00Z",
                },
            },
            dry_result={"identifier": "DRY_RUN"},
        )
        identifier = text(made, "identifier", "the new iteration")
        self._live.iterations[gap.data["name"]] = Node(gap.data["name"], "", identifier)

    def select_iteration(self, gap: Gap) -> None:
        node = self._live.iterations.get(gap.data["name"])
        if node is None:
            raise MalformedDataError(f"iteration {gap.data['name']!r} was not found after it was made")
        self._client.write(
            "POST",
            [self._live.project, azure_of(self._board).team, "_apis", "work", "teamsettings", "iterations"],
            {"id": node.identifier},
        )

    def add_folder(self, gap: Gap) -> None:
        del gap
        self._client.write(
            "POST",
            [self._live.project, "_apis", "wit", "queries", SHARED_QUERIES],
            {"name": azure_of(self._board).queries_folder, "isFolder": True},
        )

    def add_query(self, gap: Gap) -> None:
        self._client.write(
            "POST",
            [self._live.project, "_apis", "wit", "queries", SHARED_QUERIES, azure_of(self._board).queries_folder],
            {"name": gap.data["name"], "wiql": gap.data["wiql"]},
        )

    def make(self, gap: Gap) -> None:
        actions = {
            "state": self.add_state,
            "field": self.add_field,
            "field_on_type": self.add_field_to_type,
            "area": self.add_area,
            "iteration": self.add_iteration,
            "team_iteration": self.select_iteration,
            "folder": self.add_folder,
            "query": self.add_query,
        }
        actions[gap.kind](gap)
