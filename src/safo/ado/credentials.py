# SPDX-License-Identifier: Apache-2.0
"""Where an Azure DevOps token comes from: $SAFO_ADO_TOKEN, else the caller's own `az` login.

The same order as the GitHub side (a token handed in, else the person's own CLI login), and the same care with the CLI:
a fixed argument list and no shell, an `az` found only on absolute PATH entries outside the working tree, stdin closed,
stderr discarded, stdout accepted only as one plain printable token.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from safo.ado.client import ADO_URL, BEARER, PAT
from safo.credentials import PLAIN_TOKEN, find_executable
from safo.errors import ConfigError

AZ = "az"  # a bearer token minted by `az account get-access-token`
# The application id of Azure DevOps, the resource an Entra ID token must be issued for
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
AZ_TIMEOUT_SECONDS = 60.0  # az starts slowly; it may also refresh a token


class NoAdoTokenError(ConfigError):
    """No Azure DevOps credential was found."""


@dataclass(frozen=True)
class AdoCredentials:
    token: str
    kind: str  # pat | bearer: how the client sends it
    source: str  # env | az

    def __repr__(self) -> str:
        return f"AdoCredentials(kind={self.kind!r}, source={self.source!r})"


def from_env(env: Mapping[str, str]) -> AdoCredentials | None:
    """$SAFO_ADO_TOKEN, sent as `$SAFO_ADO_TOKEN_KIND` (pat, the default, or bearer for $(System.AccessToken))."""
    token = env.get("SAFO_ADO_TOKEN", "").strip()
    if not token:
        return None
    if not PLAIN_TOKEN.fullmatch(token):
        raise ConfigError("SAFO_ADO_TOKEN is not one plain printable token")
    kind = env.get("SAFO_ADO_TOKEN_KIND", PAT).strip().lower() or PAT
    if kind not in (PAT, BEARER):
        raise ConfigError(f"SAFO_ADO_TOKEN_KIND {kind!r} is not one of {PAT}, {BEARER}")
    return AdoCredentials(token, kind, "env")


def from_az(env: Mapping[str, str]) -> AdoCredentials | None:
    """An Entra ID token for Azure DevOps from the caller's `az login`, or None when az is absent or not logged in."""
    az = find_executable("az", env)
    if az is None:
        return None
    argv = [az, "account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken", "--output", "tsv"]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, absolute executable, no shell
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=AZ_TIMEOUT_SECONDS,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    token = done.stdout.strip()
    if done.returncode != 0 or not PLAIN_TOKEN.fullmatch(token):
        return None
    return AdoCredentials(token, BEARER, AZ)


def base_url(env: Mapping[str, str], creds: AdoCredentials) -> str:
    """https://dev.azure.com, or $SAFO_ADO_URL (Azure DevOps Server) for a token that was handed in.

    A token from `az` is the person's own Entra ID login: it goes to exactly one place, whatever the environment says.
    """
    url = env.get("SAFO_ADO_URL", "").strip()
    if not url or url.rstrip("/") == ADO_URL:
        return ADO_URL
    if creds.source == AZ:
        raise ConfigError(f"an az token is only sent to {ADO_URL}; SAFO_ADO_URL cannot redirect it")
    return url


def resolve(env: Mapping[str, str]) -> AdoCredentials:
    creds = from_env(env) or from_az(env)
    if creds is None:
        raise NoAdoTokenError(
            "no Azure DevOps token: set SAFO_ADO_TOKEN (a personal access token, or $(System.AccessToken) with "
            "SAFO_ADO_TOKEN_KIND=bearer in Azure Pipelines), or log in with `az login`"
        )
    return creds
