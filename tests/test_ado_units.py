# SPDX-License-Identifier: Apache-2.0
"""Azure DevOps pieces on their own: the board file, the REST client's rules, the credentials, AB#n references."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from fakeado import FakeAdo, serve
from safo import tracking
from safo.ado import credentials
from safo.ado.client import BEARER, AdoClient, check_base_url
from safo.ado.config import reference_for
from safo.errors import ApiError, AuthError, ConfigError, NotFoundError, RateLimitedError, UnknownOutcomeError
from safo.schema import Board, parse_board

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "azure-devops.yaml"
Change = Callable[[dict[str, Any]], object]


def example() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text())
    return data


def parse(data: dict[str, Any]) -> Board:
    return parse_board(data, "board.yaml")


# -- the board file ---------------------------------------------------------------------------------------------------


def test_the_example_parses() -> None:
    board = parse(example())
    assert board.project.azure and board.project.owner == "acme" and board.project.title == "Platform"
    assert board.azure is not None
    assert board.azure.team == "Platform Team"
    assert board.azure.work_item_types == ("User Story", "Bug")
    assert board.repositories[0].full_name == "Platform/api"  # owner defaults to the project
    assert board.repositories[1].full_name == "Shared/pipelines"


def test_reference_names() -> None:
    board = parse(example())
    assert board.azure is not None
    assert reference_for(board.azure, "Done on") == "Custom.DoneOn"
    assert reference_for(board.azure, "Agent") == "Custom.Agent"


SECOND_ITERATION = {"name": "Other", "type": "iteration", "start": "2026-10-20", "duration_days": 7, "count": 2}
REFUSALS: list[tuple[Change, str]] = [
    (lambda d: d.pop("azure_devops"), "azure_devops: required"),
    (lambda d: d["project"].update(owner="-bad"), "not an Azure DevOps organization name"),
    (lambda d: d["project"].update(title="a/b"), "not an Azure DevOps project name"),
    (lambda d: d["project"].update(number=3), "unknown key"),
    (lambda d: d["repositories"][0].update(default_area="Core"), "put default_area and area_rules under"),
    (lambda d: d["repositories"][0].update(name="../x"), "not an Azure Repos repository name"),
    (lambda d: d["azure_devops"].update(work_item_types=[]), "between 1 and 16"),
    (lambda d: d["azure_devops"].update(work_item_types=["Bug", "bug"]), "listed twice"),
    (lambda d: d["azure_devops"].update(default_area="Nowhere"), "not an option of the area field"),
    (lambda d: d["azure_devops"].update(state_categories={"Closed": "InProgress"}), "must be Completed"),
    (lambda d: d["azure_devops"].update(state_categories={"Blocked": "Stuck"}), "is not one of"),
    (lambda d: d["azure_devops"].update(field_refs={"Agent": "not a ref"}), "not a field reference name"),
    (lambda d: d["azure_devops"].update(field_refs={"Agent": "Custom.Kind"}), "same reference name"),
    (lambda d: d["azure_devops"].update(field_refs={"State": "Custom.State"}), "state, area or iteration"),
    (lambda d: d["azure_devops"].update(status_page="a/../b"), "not a wiki page path"),
    (lambda d: d["azure_devops"].update(closed_days=4000), "at most 3650"),
    (lambda d: d["fields"].append(SECOND_ITERATION), "one iteration field"),
    (lambda d: d["fields"][0]["options"].append({"name": "Won't do"}), "not a state name"),
    (lambda d: d["views"].append({"name": "a/b", "layout": "table"}), "not a query name"),
    (lambda d: d.update(process={"method": "waterfall"}), "process"),
]


@pytest.mark.parametrize(("change", "message"), REFUSALS)
def test_the_board_file_refuses(change: Change, message: str) -> None:
    data = example()
    change(data)
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse(data)


def test_a_github_board_refuses_the_azure_block() -> None:
    data = {
        "version": 1,
        "project": {"owner": "acme", "owner_type": "organization", "title": "x"},
        "repositories": [],
        "azure_devops": {"work_item_types": ["Bug"]},
    }
    with pytest.raises(ConfigError, match=re.escape("only a board with project.platform: azure-devops")):
        parse(data)


def test_an_unknown_platform_is_refused() -> None:
    data = example()
    data["project"]["platform"] = "gitlab"
    with pytest.raises(ConfigError, match="is not one of github, azure-devops"):
        parse(data)


# -- the client ------------------------------------------------------------------------------------------


@pytest.fixture
def fake() -> Iterator[tuple[FakeAdo, str]]:
    f = FakeAdo()
    with serve(f) as url:
        yield f, url


def client(url: str, **kw: Any) -> AdoClient:
    return AdoClient("pat-secret", "acme", base_url=url, sleep=lambda _: None, **kw)


AREAS = ["Platform", "_apis", "wit", "classificationnodes", "Areas"]


def test_every_segment_is_encoded_on_its_own(fake: tuple[FakeAdo, str]) -> None:
    _, url = fake
    made = client(url).url(["Platform", "_apis", "wit", "queries", "Shared Queries", "a/b?c"], {"$depth": 1})
    assert "/Shared%20Queries/a%2Fb%3Fc?" in made
    assert made.endswith("api-version=7.1&%24depth=1")
    with pytest.raises(ConfigError):
        client(url).url(["..", "x"])


def test_base_url_rules() -> None:
    check_base_url("https://dev.azure.com")
    check_base_url("https://tfs.example.test/tfs")
    check_base_url("http://127.0.0.1:8080")
    for bad in ("http://dev.azure.com", "https://user:pw@dev.azure.com", "https://dev.azure.com?x=1"):
        with pytest.raises(ConfigError):
            check_base_url(bad)


def test_a_pat_is_sent_as_basic_and_a_bearer_as_bearer(fake: tuple[FakeAdo, str]) -> None:
    f, url = fake
    client(url).get(["_apis", "wit", "fields"])
    AdoClient("tok", "acme", kind=BEARER, base_url=url).get(["_apis", "wit", "fields"])
    assert "Basic OnBhdC1zZWNyZXQ=" in f.seen_auth  # base64(":pat-secret")
    assert "Bearer tok" in f.seen_auth


@pytest.mark.parametrize("status", [203, 302])
def test_a_sign_in_page_or_redirect_is_a_refused_token(fake: tuple[FakeAdo, str], status: int) -> None:
    f, url = fake
    f.fail("GET", "_apis/wit/fields", status, {"x": 1}, headers={"Location": "https://login.example.test/"})
    with pytest.raises(AuthError, match="sign-in page"):
        client(url).get(["_apis", "wit", "fields"])


def test_html_is_never_read_as_an_answer(fake: tuple[FakeAdo, str]) -> None:
    f, url = fake
    f.fail("GET", "_apis/wit/fields", 200, "<html>", headers={"Content-Type": "text/html"})
    with pytest.raises(AuthError, match="not JSON"):
        client(url).get(["_apis", "wit", "fields"])


def test_a_read_is_retried_and_a_404_is_not_found(fake: tuple[FakeAdo, str]) -> None:
    f, url = fake
    f.fail("GET", "_apis/wit/fields", 503)
    f.fail("GET", "_apis/wit/fields", 429, headers={"Retry-After": "1"})
    assert client(url).get(["_apis", "wit", "fields"])["count"] > 0
    with pytest.raises(NotFoundError):
        client(url).get(["Platform", "_apis", "wit", "workitemtypes", "Epic"])


def test_a_read_rate_limit_that_never_clears(fake: tuple[FakeAdo, str]) -> None:
    f, url = fake
    for _ in range(5):
        f.fail("GET", "_apis/wit/fields", 429, headers={"Retry-After": "1"})
    with pytest.raises(RateLimitedError):
        client(url).get(["_apis", "wit", "fields"])


def test_a_write_is_never_resent_after_it_went_out(fake: tuple[FakeAdo, str]) -> None:
    f, url = fake
    f.fail("POST", "classificationnodes/Areas", 500)
    with pytest.raises(UnknownOutcomeError) as caught:
        client(url).write("POST", AREAS, {"name": "x"})
    assert "pat-secret" not in str(caught.value)
    assert sum(1 for m, _ in f.log if m == "POST") == 1


def test_a_throttled_write_is_retried_and_a_refusal_is_not(fake: tuple[FakeAdo, str]) -> None:
    f, url = fake
    f.fail("POST", "classificationnodes/Areas", 429, headers={"Retry-After": "1"})
    c = client(url)
    c.write("POST", AREAS, {"name": "x"})
    assert "x" in f.areas
    with pytest.raises(ApiError, match="refused") as caught:
        c.write("POST", AREAS, {"name": "x"})
    assert not isinstance(caught.value, UnknownOutcomeError)


def test_a_dry_run_sends_no_write(fake: tuple[FakeAdo, str]) -> None:
    f, url = fake
    c = client(url, dry_run=True)
    assert c.write("POST", ["Platform", "_apis", "wit", "fields"], {"a": 1}, dry_result={"id": "d"}) == {"id": "d"}
    assert f.log == [] and c.skipped == 1


def test_only_writes_are_writes(fake: tuple[FakeAdo, str]) -> None:
    _, url = fake
    with pytest.raises(ConfigError):
        client(url).write("DELETE", ["Platform", "_apis", "wit", "workitems", "1"], None)


def test_a_server_message_never_carries_the_token(fake: tuple[FakeAdo, str]) -> None:
    f, url = fake
    f.fail("POST", "classificationnodes/Areas", 400, {"message": "bad pat-secret\n::set-output x"})
    with pytest.raises(ApiError) as caught:
        client(url).write("POST", AREAS, {"name": "x"})
    assert "pat-secret" not in str(caught.value) and "\n" not in str(caught.value)


# -- credentials -----------------------------------------------------------------------------------------


def test_the_token_from_the_environment() -> None:
    creds = credentials.from_env({"SAFO_ADO_TOKEN": "abc"})
    assert creds is not None and creds.kind == "pat" and "abc" not in repr(creds)
    bearer = credentials.from_env({"SAFO_ADO_TOKEN": "abc", "SAFO_ADO_TOKEN_KIND": "bearer"})
    assert bearer is not None and bearer.kind == "bearer"
    assert credentials.from_env({}) is None
    with pytest.raises(ConfigError):
        credentials.from_env({"SAFO_ADO_TOKEN": "abc", "SAFO_ADO_TOKEN_KIND": "cookie"})
    with pytest.raises(ConfigError):
        credentials.from_env({"SAFO_ADO_TOKEN": "a b"})


def _fake_az(tmp_path: Path, script: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    az = bin_dir / "az"
    az.write_text("#!/bin/sh\n" + script)
    az.chmod(az.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}


def test_the_token_from_az(tmp_path: Path) -> None:
    env = _fake_az(tmp_path, 'echo "$@" > "$(dirname "$0")/args"; echo eyJ.token.value\n')
    creds = credentials.from_az(env)
    assert creds is not None and creds.kind == "bearer" and creds.source == "az"
    args = (tmp_path / "bin" / "args").read_text()
    assert "--resource 499b84ac-1321-427f-aa17-267ca6975798" in args


def test_az_not_logged_in_is_no_token(tmp_path: Path) -> None:
    env = _fake_az(tmp_path, "echo 'Please run az login' >&2; exit 1\n")
    assert credentials.from_az(env) is None
    with pytest.raises(credentials.NoAdoTokenError):
        credentials.resolve(env)


def test_an_az_token_never_goes_elsewhere() -> None:
    az = credentials.AdoCredentials("t", "bearer", "az")
    pat = credentials.AdoCredentials("t", "pat", "env")
    server = {"SAFO_ADO_URL": "https://tfs.example.test/tfs"}
    assert credentials.base_url(server, pat) == "https://tfs.example.test/tfs"
    with pytest.raises(ConfigError, match=re.escape("only sent to https://dev.azure.com")):
        credentials.base_url(server, az)
    assert credentials.base_url({}, az) == "https://dev.azure.com"


# -- the dispatch guard's references ---------------------------------------------------------------------


def test_an_azure_boards_reference_is_accepted_on_its_shape() -> None:
    ref = tracking.parse("Do the thing.\nissue: AB#1234\n")
    assert ref is not None and ref.tracker == "azure" and ref.number == 1234

    def never(_: tracking.Ref) -> tracking.State:
        raise AssertionError("a malformed reference is never looked up")

    resolver = tracking.gh_resolver(lambda *a, **k: None, lambda _: None)
    assert tracking.check("issue: AB#7", resolver) is None
    assert tracking.check("issue: AB#0", never) is not None
    assert tracking.check("issue: ab#7", never) is not None
