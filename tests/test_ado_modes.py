# SPDX-License-Identifier: Apache-2.0
"""The board modes on an Azure DevOps board, end to end through the command line, against the loopback fake."""

from __future__ import annotations

import datetime as dt
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fakeado import PROJECT, FakeAdo, serve
from safo.ado.client import AdoClient
from safo.cli import main
from safo.schema import Board

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "azure-devops.yaml"
TODAY = dt.date(2026, 10, 7)


class World:
    def __init__(self, fake: FakeAdo, url: str) -> None:
        self.fake = fake
        self.url = url

    def run(self, *argv: str, board: Path = EXAMPLE, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()

        def factory(b: Board, e: Any, dry: bool) -> AdoClient:
            return AdoClient("pat-secret", b.project.owner, base_url=self.url, dry_run=dry, sleep=lambda _: None)

        def no_github(*_: Any) -> Any:
            raise AssertionError("an Azure DevOps board never builds a GitHub client")

        code = main(
            ["--board", str(board), *argv],
            env=env or {},
            client_factory=no_github,
            ado_client_factory=factory,
            out=out,
            err=err,
            today=TODAY,
        )
        return code, out.getvalue(), err.getvalue()

    def repos(self) -> None:
        for name, project in (("api", PROJECT), ("pipelines", "Shared")):
            self.fake.add_gate(self.fake.add_repo(name, project))

    def ready(self) -> None:
        """Bootstrap the fake to zero structural drift, with repositories that pass the gate."""
        self.repos()
        code, out, err = self.run("bootstrap")
        assert code == 0, out + err


@pytest.fixture
def world() -> Iterator[World]:
    fake = FakeAdo()
    fake.today = TODAY
    with serve(fake) as url:
        yield World(fake, url)


def test_validate_reads_nothing(world: World) -> None:
    code, out, _ = world.run("validate")
    assert code == 0
    assert "configuration valid" in out
    assert world.fake.log == []


def test_bootstrap_makes_the_structure_and_a_second_run_finds_nothing(world: World) -> None:
    world.repos()
    code, out, err = world.run("bootstrap")
    assert code == 0, out + err
    fake = world.fake
    assert {"Core", "Docs", "CI and release"} <= set(fake.areas)
    assert [f"Sprint {i}" for i in range(1, 7)] == list(fake.iterations)
    assert fake.iterations["Sprint 1"]["attributes"] == {
        "startDate": "2026-10-20T00:00:00Z",
        "finishDate": "2026-11-02T00:00:00Z",
    }
    assert len(fake.teams["Platform Team"]) == 6
    story = fake.types["User Story"]
    assert {"name": "Blocked", "category": "InProgress", "color": "cc293d"} in story["states"]
    assert fake.process_types["User Story"]["customization"] == "inherited"  # derived before the first change
    assert {"custom.agent", "custom.kind", "custom.effort", "custom.epic", "custom.doneon"} <= story["fields"]
    agent = fake.fields["custom.agent"]
    assert agent["isPicklist"] and fake.picklists[agent["picklistId"]] == ["Claude", "Codex", "Copilot", "You"]
    assert fake.fields["custom.doneon"]["type"] == "dateTime"
    assert fake.queries is not None and set(fake.queries) == {"Open bugs", "Blocked"}
    assert "[System.State] = 'Blocked'" in fake.queries["Blocked"]["wiql"]
    assert "CREATED area 'Docs'" in out

    before = len(fake.writes())
    code, out, _ = world.run("bootstrap")
    assert code == 0
    assert "nothing to create" in out
    assert len(fake.writes()) == before


def test_bootstrap_dry_run_sends_nothing(world: World) -> None:
    code, out, _ = world.run("--dry-run", "bootstrap")
    assert code == 1
    assert "WOULD CREATE" in out
    assert world.fake.writes() == []


def test_bootstrap_on_a_system_process_leaves_states_and_fields(world: World) -> None:
    world.fake.process["customizationType"] = "system"
    code, out, _ = world.run("bootstrap")
    assert code == 1
    assert "LEFT    work item type 'User Story' has no state 'Blocked'" in out
    assert "is a system process" in out
    assert "Blocked" not in {s["name"] for s in world.fake.types["User Story"]["states"]}
    assert "Docs" in world.fake.areas  # what the project itself holds is still made


def test_bootstrap_never_edits_an_existing_picklist(world: World) -> None:
    world.fake.picklists["list-1"] = ["Claude"]
    world.fake.fields["custom.agent"] = {
        "name": "Agent",
        "referenceName": "Custom.Agent",
        "type": "string",
        "isPicklist": True,
        "picklistId": "list-1",
    }
    code, out, _ = world.run("bootstrap")
    assert code == 1
    assert "lacks 'Codex', 'Copilot', 'You'" in out
    assert world.fake.picklists["list-1"] == ["Claude"]


def test_bootstrap_stops_after_an_unknown_outcome(world: World) -> None:
    world.fake.fail("POST", "classificationnodes/Areas", 500)
    code, out, _ = world.run("bootstrap")
    assert code == 2
    assert "UNKNOWN" in out and "stopped: nothing more was sent" in out
    assert not any("Iterations" in p for _, p in world.fake.writes())
    assert sum(1 for m, p in world.fake.log if m == "POST" and "Areas" in p) == 1  # never resent


def test_audit_is_clean_after_bootstrap_and_writes_nothing(world: World) -> None:
    world.ready()
    before = len(world.fake.writes())
    code, out, err = world.run("audit")
    assert code == 0, out + err
    assert out.strip().endswith("clean")
    assert len(world.fake.writes()) == before


def test_audit_reports_structure(world: World) -> None:
    world.repos()
    world.fake.add_item(1, title="write docs")
    code, out, _ = world.run("audit")
    assert code == 1
    assert "DRIFT   area 'Docs' is missing under Platform" in out
    assert "DRIFT   field 'Agent' (Custom.Agent, string, picklist) does not exist in the organization" in out
    assert "DRIFT   work item type 'Bug' has no state 'Blocked' (InProgress)" in out
    assert "DRIFT   shared query folder 'SAFO' is missing" in out or "DRIFT   the shared query folder" in out


def test_audit_cannot_tell_about_an_unreadable_repository(world: World) -> None:
    world.fake.add_repo("api")
    code, out, _ = world.run("audit")
    assert code == 2
    assert "UNKNOWN repository Shared/pipelines cannot be read with this token" in out


def test_reconcile_fills_area_default_and_done_date(world: World) -> None:
    world.ready()
    fake = world.fake
    fake.add_item(1, title="Update the docs index")
    fake.add_item(2, title="Fix login", **{"Custom.Agent": "Codex"})
    fake.add_item(3, state="Closed", **{"Microsoft.VSTS.Common.ClosedDate": "2026-10-05T16:00:00Z"})
    fake.add_item(4, title="a pipeline step", area=f"{PROJECT}\\Core")
    code, out, err = world.run("reconcile")
    assert code == 1, out + err  # Kind, Epic and Effort are missing: a person must pick them
    one = fake.items[1]["fields"]
    assert one["System.AreaPath"] == f"{PROJECT}\\Docs"
    assert one["Custom.Agent"] == "You"
    assert fake.items[2]["fields"]["Custom.Agent"] == "Codex"  # a value a person chose is never overwritten
    assert fake.items[3]["fields"]["Custom.DoneOn"] == "2026-10-05T00:00:00Z"
    assert fake.items[4]["fields"]["System.AreaPath"] == f"{PROJECT}\\Core"  # an area already set is left
    assert "UPDATED AB#1: Area = Docs; Agent = You" in out
    assert "HUMAN AB#1 has no Kind, Epic, Effort" in out
    assert fake.items[1]["rev"] == 2

    code, out, _ = world.run("reconcile")
    assert "UPDATED" not in out


def test_reconcile_reports_a_revision_conflict(world: World) -> None:
    world.ready()
    world.fake.add_item(1, title="docs")
    world.fake.fail("PATCH", "workitems/1", 412, {"message": "TF26071: changed by someone else"})
    code, out, _ = world.run("reconcile")
    assert code == 1
    assert "FAILED AB#1" in out and "TF26071" in out


def test_reconcile_tests_the_revision_it_read(world: World) -> None:
    world.ready()
    world.fake.add_item(1, title="docs")
    world.fake.items[1]["rev"] = 7
    world.run("reconcile")
    assert world.fake.items[1]["rev"] == 8  # the patch carried `test /rev 7` and was applied


def test_reconcile_stops_on_an_unknown_outcome_and_observes(world: World) -> None:
    world.ready()
    world.fake.add_item(1, title="docs")
    world.fake.add_item(2, title="docs too")
    world.fake.fail("PATCH", "workitems/1", 504, apply=True)
    code, out, _ = world.run("reconcile")
    assert code == 2
    assert "UNKNOWN AB#1" in out
    assert "OBSERVED AB#1 is now at revision 2 (was 1)" in out
    assert "NOT ATTEMPTED AB#2" in out
    assert world.fake.items[2]["rev"] == 1
    assert sum(1 for m, _ in world.fake.log if m == "PATCH") == 1  # never resent


def test_reconcile_dry_run_writes_nothing(world: World) -> None:
    world.ready()
    world.fake.add_item(1, title="docs")
    code, out, _ = world.run("--dry-run", "reconcile")
    assert code == 1
    assert "WOULD UPDATE AB#1" in out
    assert world.fake.items[1]["rev"] == 1


def test_reconcile_lists_pull_requests_without_work_items(world: World) -> None:
    world.ready()
    api = world.fake.repos["platform/api"]
    world.fake.add_pull(api, 7)
    world.fake.add_pull(api, 8, links=[1])
    world.fake.add_pull(api, 9, author="dependabot@acme.test")
    code, out, _ = world.run("reconcile")
    assert code == 1
    assert "HUMAN pull request Platform/api!7 links no work item" in out
    assert "!8" not in out and "!9" not in out


def test_reconcile_notes_a_state_the_board_does_not_know(world: World) -> None:
    world.ready()
    world.fake.types["User Story"]["states"].append({"name": "Parked", "category": "InProgress", "color": "x"})
    world.fake.add_item(2, state="Parked", area=f"{PROJECT}\\Core")
    code, out, _ = world.run("reconcile")
    assert code == 1
    assert "NOTE AB#2 is in the state 'Parked', which is not an option of 'State'" in out


def test_reconcile_never_reads_a_removed_item(world: World) -> None:
    world.ready()
    world.fake.add_item(1, state="Removed", title="docs")
    code, out, _ = world.run("reconcile")
    assert code == 0
    assert "AB#1" not in out
    assert world.fake.items[1]["rev"] == 1


def test_sync_clears_the_done_date_of_a_reopened_item(world: World, tmp_path: Path) -> None:
    world.ready()
    world.fake.add_item(5, state="Active", area=f"{PROJECT}\\Core", **{"Custom.DoneOn": "2026-10-01T00:00:00Z"})
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"eventType": "workitem.updated", "resource": {"workItemId": 5}}))
    code, out, _ = world.run("sync", "--event-path", str(event))
    assert "UPDATED AB#5: Done on cleared" in out
    assert "Custom.DoneOn" not in world.fake.items[5]["fields"]
    assert code == 1  # Kind, Epic, Effort still need a person


def test_sync_by_work_item_number(world: World) -> None:
    world.ready()
    world.fake.add_item(6, title="docs")
    _, out, _ = world.run("sync", "--work-item", "6")
    assert "UPDATED AB#6: Area = Docs; Agent = You" in out
    code, out, _ = world.run("sync", "--work-item", "99")
    assert code == 2 and "VANISHED AB#99" in out


def test_sync_a_pull_request_event(world: World, tmp_path: Path) -> None:
    world.ready()
    api = world.fake.repos["platform/api"]
    world.fake.add_pull(api, 7)
    event = tmp_path / "event.json"
    resource = {"pullRequestId": 7, "repository": {"name": "api", "project": {"name": PROJECT}}}
    event.write_text(json.dumps({"eventType": "git.pullrequest.created", "resource": resource}))
    code, out, _ = world.run("sync", "--event-path", str(event))
    assert code == 1
    assert "HUMAN pull request Platform/api!7 links no work item" in out
    api.links[7] = [3]
    code, out, _ = world.run("sync", "--event-path", str(event))
    assert code == 0 and "linked work items AB#3" in out


def test_sync_an_unlisted_repository_reads_nothing(world: World, tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    resource = {"pullRequestId": 7, "repository": {"name": "other", "project": {"name": PROJECT}}}
    event.write_text(json.dumps({"eventType": "git.pullrequest.updated", "resource": resource}))
    code, out, _ = world.run("sync", "--event-path", str(event))
    assert code == 1 and "not listed in board.yaml" in out
    assert world.fake.log == []


def test_sync_ignores_other_events(world: World, tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"eventType": "build.complete", "resource": {}}))
    code, out, _ = world.run("sync", "--event-path", str(event))
    assert code == 0 and "nothing to do" in out


def test_status_post_writes_a_wiki_page_once(world: World) -> None:
    world.ready()
    world.fake.add_item(1, state="Active", title="ship it", **{"Custom.Agent": "Claude"})
    world.fake.add_item(2, state="Closed", **{"Microsoft.VSTS.Common.ClosedDate": "2026-10-06T10:00:00Z"})
    code, out, err = world.run("status", "--post", "--no-local")
    assert code == 0, out + err
    page = world.fake.pages["/SAFO Status/2026-10-07"]
    assert page.startswith("State: ON_TRACK")
    assert "ship it" in page
    assert "/SAFO Status" in world.fake.pages
    code, out, _ = world.run("status", "--post", "--no-local")
    assert code == 1 and "already exists" in out


def test_status_print_sends_nothing(world: World) -> None:
    world.ready()
    world.fake.add_item(1, state="Active")
    before = len(world.fake.writes())
    code, out, _ = world.run("status", "--post", "--print", "--no-local")
    assert code == 0
    assert "::stop-commands::" in out
    assert len(world.fake.writes()) == before


def test_status_without_a_wiki_says_how(world: World) -> None:
    world.ready()
    world.fake.wiki = None
    code, _, err = world.run("status", "--post", "--no-local")
    assert code == 2 and "no project wiki" in err


def test_agents_status_reads_the_agent_field(world: World) -> None:
    world.ready()
    world.fake.add_item(1, state="Active", title="build it", **{"Custom.Agent": "Codex"})
    world.fake.add_item(2, state="Blocked", title="decide", **{"Custom.Agent": "You"})
    code, out, _ = world.run("agents-status", "--no-guard", "--no-codex", "--no-copilot")
    assert code == 0
    assert "Codex        Active       AB#1  build it" in out
    assert "You          AB#2  decide" in out


def test_gate_drift_when_the_policy_is_not_blocking(world: World) -> None:
    world.ready()
    world.fake.policies[0]["isBlocking"] = False
    code, out, _ = world.run("audit")
    assert code == 1
    assert "DRIFT   Platform/api: refs/heads/main has no enabled, blocking build validation" in out


def test_a_refused_token_is_one_line(world: World) -> None:
    world.fake.fail("GET", "_apis/projects", 401)
    code, out, err = world.run("audit")
    assert code == 2
    assert "refused the token (401)" in err
    assert "pat-secret" not in out + err


def test_no_token_on_a_dry_run_is_unknown() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(["--board", str(EXAMPLE), "--dry-run", "audit"], env={"PATH": ""}, out=out, err=err)
    assert code == 2
    assert "no token" in out.getvalue()


def test_no_token_is_an_error_that_names_both_ways() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(["--board", str(EXAMPLE), "audit"], env={"PATH": ""}, out=out, err=err)
    assert code == 2
    assert "SAFO_ADO_TOKEN" in err.getvalue() and "az login" in err.getvalue()
