# SPDX-License-Identifier: Apache-2.0
"""The Azure DevOps REST client: the same rules as safo.graphql.Client, for REST.

* The token stays inside this object: never in a message, a log line or an exception.
* Every request goes to the one base URL (https://dev.azure.com, or loopback for the tests); redirects are refused, so
  the token never follows one. Azure DevOps answers a bad credential with a redirect or a 203 sign-in page, and both
  are reported as a refused token.
* A response body is capped, framed strictly and must be JSON (safo.graphql's reader).
* A read (`get`, and `query` for the POST endpoints that only read: WIQL, the work item batch) is retried on a
  transient failure. A write is never sent twice: only a refusal that provably ran nothing (a connection that failed
  before any byte left, 401, a 429 with Retry-After) lets it be retried; a 4xx the server gave a reason for is a
  refusal (ApiError); a timeout, a 5xx or an unreadable answer after the request went out is UnknownOutcomeError, and
  the caller must re-read before deciding anything.
* A dry run sends no write at all and prints what it would have sent.
* Every path segment is percent-encoded here, so a name from board.yaml can never add a segment or a query string.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, TextIO

from safo import __version__, output
from safo.errors import (
    ApiError,
    AuthError,
    ConfigError,
    MalformedDataError,
    NotFoundError,
    RateLimitedError,
    UnknownOutcomeError,
)
from safo.graphql import LOOPBACK, _Connected, _http_handler, _https_handler, _lower, _NoRedirect, _read

ADO_URL = "https://dev.azure.com"
API_VERSION = "7.1"
PAT = "pat"  # a personal access token: HTTP Basic with an empty user name
BEARER = "bearer"  # an Entra ID access token (az login), or $(System.AccessToken) in Azure Pipelines
MAX_PAGES = 1000
MAX_MESSAGE = 300
REFUSALS = (400, 403, 404, 409, 412)  # a reasoned refusal: the write was validated and not applied
Query = Mapping[str, str | int]


def check_base_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError("the Azure DevOps URL must be a plain base URL (no query, fragment or credentials)")
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return
    raise ConfigError(f"the Azure DevOps URL {url!r} must be https (plain http is accepted for loopback only)")


class _Transport(Exception):
    def __init__(self, error: Exception, *, before_send: bool) -> None:
        super().__init__(error.__class__.__name__)
        self.error = error
        self.before_send = before_send


def _where(segments: Sequence[str]) -> str:
    return "/".join(segments[-3:])


class AdoClient:
    """REST calls against one organization: `get`, `query` (a read sent as POST), `write`."""

    def __init__(
        self,
        token: str,
        organization: str,
        *,
        kind: str = PAT,
        base_url: str = ADO_URL,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        max_wait: float = 120.0,
        timeout: float = 60.0,
        out: TextIO | None = None,
    ) -> None:
        check_base_url(base_url)
        if kind not in (PAT, BEARER):
            raise ConfigError(f"an Azure DevOps token is {PAT} or {BEARER}, not {kind!r}")
        self._token = token
        self._kind = kind
        self._base = base_url.rstrip("/")
        self.organization = organization
        self.dry_run = dry_run
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._max_wait = max_wait
        self._timeout = timeout
        self._out = out or sys.stdout
        self.requests = 0
        self.skipped = 0

    def __repr__(self) -> str:
        return f"AdoClient(url={self._base!r}, organization={self.organization!r}, dry_run={self.dry_run})"

    # -- addresses ---------------------------------------------------------------

    def url(self, segments: Sequence[str], params: Query | None = None, api_version: str = API_VERSION) -> str:
        """`<base>/<organization>/<segment>/...?api-version=...`, every segment percent-encoded on its own."""
        for segment in segments:
            if not isinstance(segment, str) or not segment or segment in (".", ".."):
                raise ConfigError("an Azure DevOps path segment is empty or a dot segment")
        path = "/".join(urllib.parse.quote(s, safe="") for s in (self.organization, *segments))
        query = {"api-version": api_version, **{k: str(v) for k, v in (params or {}).items()}}
        return f"{self._base}/{path}?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}"

    # -- reads -----------------------------------------------------------------------

    def get(self, segments: Sequence[str], params: Query | None = None, *, api_version: str = API_VERSION) -> Any:
        return self._read("GET", segments, params, None, api_version)[1]

    def query(
        self, segments: Sequence[str], body: Any, params: Query | None = None, *, api_version: str = API_VERSION
    ) -> Any:
        """A POST that only reads (WIQL, the work item batch): safe to retry, never skipped by a dry run."""
        return self._read("POST", segments, params, body, api_version)[1]

    def values(
        self, segments: Sequence[str], params: Query | None = None, *, api_version: str = API_VERSION
    ) -> Iterator[Any]:
        """Every entry of a `{"value": [...]}` list, following `x-ms-continuationtoken` to the end."""
        token: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            page_params: dict[str, str | int] = dict(params or {})
            if token is not None:
                page_params["continuationToken"] = token
            headers, body = self._read("GET", segments, page_params, None, api_version)
            yield from value_list(body, _where(segments))
            token = headers.get("x-ms-continuationtoken") or None
            if token is None:
                return
            if token in seen:
                raise ApiError(f"Azure DevOps repeated a continuation token for {_where(segments)}")
            seen.add(token)
        raise ApiError(f"{_where(segments)}: more than {MAX_PAGES} pages")

    def _read(
        self, method: str, segments: Sequence[str], params: Query | None, body: Any, api_version: str
    ) -> tuple[Mapping[str, str], Any]:
        what = f"{method} {_where(segments)}"
        url = self.url(segments, params, api_version)
        payload = None if body is None else json.dumps(body).encode()
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            try:
                status, headers, raw = self._send(method, url, payload, "application/json")
            except _Transport as err:
                if last:
                    raise ApiError(f"could not reach Azure DevOps for {what}: {err.error.__class__.__name__}") from None
                self._sleep(min(self._max_wait, 2.0**attempt))
                continue
            self.requests += 1
            self._refused_token(status, what)
            if status == 429 or (status == 503 and "retry-after" in headers):
                wait = _retry_after(headers)
                if last or wait > self._max_wait:
                    raise RateLimitedError(f"{what}: still rate limited after {attempt} attempts")
                self._sleep(wait)
                continue
            if status in (500, 502, 503, 504):
                if last:
                    raise ApiError(f"Azure DevOps answered {status} to {what} {attempt} times")
                self._sleep(min(self._max_wait, 2.0**attempt))
                continue
            if status == 404:
                raise NotFoundError(f"{what}: not found ({self._message(raw)})")
            if status == 403:
                raise AuthError(f"Azure DevOps refused {what} (403): {self._message(raw)}")
            if status != 200:
                raise ApiError(f"Azure DevOps answered {status} to {what}: {self._message(raw)}")
            return headers, self._json(raw, headers, what)
        raise ApiError(f"Azure DevOps kept failing {what}")  # pragma: no cover - the loop always returns or raises

    # -- writes ------------------------------------------------------------------------

    def write(
        self,
        method: str,
        segments: Sequence[str],
        body: Any,
        params: Query | None = None,
        *,
        api_version: str = API_VERSION,
        content_type: str = "application/json",
        dry_result: Any = None,
    ) -> Any:
        """One write (POST, PUT, PATCH). Its JSON answer, or `dry_result` on a dry run, when nothing is sent."""
        if method not in ("POST", "PUT", "PATCH"):
            raise ConfigError(f"safo sends no {method} to Azure DevOps")
        what = f"{method} {_where(segments)}"
        url = self.url(segments, params, api_version)
        if self.dry_run:
            self.skipped += 1
            shown = json.dumps(body, sort_keys=True, default=str)
            output.write(self._out, f"dry run: would {what} {shown}")
            return {} if dry_result is None else dry_result
        payload = json.dumps(body).encode()
        for attempt in range(1, self._max_attempts + 1):
            try:
                status, headers, raw = self._send(method, url, payload, content_type)
            except _Transport as err:
                if err.before_send:
                    raise ApiError(f"could not reach Azure DevOps for {what}: {err.error.__class__.__name__}") from None
                raise self._unknown(what, "the connection failed after the request was sent") from None
            except ApiError as err:  # refused unread (too large, malformed framing): the request had gone out
                raise self._unknown(what, str(err)) from None
            self.requests += 1
            self._refused_token(status, what)
            if status == 429 and "retry-after" in headers:  # throttled: refused before it ran
                wait = _retry_after(headers)
                if attempt == self._max_attempts or wait > self._max_wait:
                    raise RateLimitedError(f"{what}: still rate limited after {attempt} attempts")
                self._sleep(wait)
                continue
            if status in REFUSALS:
                cls = NotFoundError if status == 404 else ApiError
                raise cls(f"Azure DevOps refused {what} ({status}): {self._message(raw)}")
            if status not in (200, 201):
                raise self._unknown(what, f"Azure DevOps answered {status}")
            try:
                return self._json(raw, headers, what)
            except (ApiError, AuthError) as err:
                raise self._unknown(what, str(err)) from None
        raise ApiError(f"Azure DevOps kept failing {what}")  # pragma: no cover - the loop always returns or raises

    # -- plumbing ----------------------------------------------------------------------

    @staticmethod
    def _refused_token(status: int, what: str) -> None:
        if status == 401:
            raise AuthError(f"Azure DevOps refused the token (401) on {what}")
        if status == 203 or 300 <= status < 400:
            # A sign-in page or a redirect to one: the credential was not accepted.
            raise AuthError(f"Azure DevOps answered {what} with a sign-in page ({status}): the token was not accepted")

    @staticmethod
    def _json(raw: bytes, headers: Mapping[str, str], what: str) -> Any:
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise AuthError(
                f"Azure DevOps answered {what} with {kind or 'no content type'}, not JSON (a sign-in page?)"
            )
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            raise ApiError(f"Azure DevOps answered {what} with a body that is not JSON") from None

    def _message(self, raw: bytes) -> str:
        """The server's own error message, one line, clipped, never the token."""
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            return "no message"
        text = body.get("message") if isinstance(body, dict) else None
        if not isinstance(text, str):
            return "no message"
        text = output.line(text)[:MAX_MESSAGE]
        return text.replace(self._token, "***") if self._token else text

    def _unknown(self, what: str, reason: str) -> UnknownOutcomeError:
        message = (
            f"{what}: the write may or may not have been applied ({reason}); "
            "re-read state before retrying, do not simply resend it"
        )
        return UnknownOutcomeError(message.replace(self._token, "***") if self._token else message)

    def _authorization(self) -> str:
        if self._kind == PAT:
            return "Basic " + base64.b64encode(f":{self._token}".encode()).decode()
        return f"Bearer {self._token}"

    def _send(
        self, method: str, url: str, payload: bytes | None, content_type: str
    ) -> tuple[int, Mapping[str, str], bytes]:
        headers = {
            "Authorization": self._authorization(),
            "Accept": "application/json",
            "User-Agent": f"safo/{__version__}",
            "X-TFS-FedAuthRedirect": "Suppress",  # a refused credential answers 401, not a sign-in redirect
        }
        if payload is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=payload, method=method, headers=headers)  # noqa: S310 - checked
        connected = _Connected()
        opener = urllib.request.build_opener(_NoRedirect, _http_handler(connected), _https_handler(connected))
        try:
            try:
                response = opener.open(request, timeout=self._timeout)
                with response as resp:
                    return resp.status, _lower(resp.headers.items()), _ado_read(resp)
            except urllib.error.HTTPError as err:
                return err.code, _lower(err.headers.items()), _ado_read(err)
        except (OSError, http.client.HTTPException) as err:
            raise _Transport(err, before_send=not connected.done) from None


def _ado_read(response: Any) -> bytes:
    try:
        return _read(response)
    except ApiError as err:
        raise ApiError(str(err).replace("GitHub", "Azure DevOps")) from None


def _retry_after(headers: Mapping[str, str]) -> float:
    try:
        wait = float(headers.get("retry-after", "60"))
    except ValueError:
        return 60.0
    return wait if math.isfinite(wait) and wait >= 0 else 60.0


def value_list(body: Any, what: str) -> list[Any]:
    """The `value` list of a list answer; anything else is not shaped as asked."""
    if not isinstance(body, dict) or not isinstance(body.get("value"), list):
        raise MalformedDataError(f"{what}: Azure DevOps did not answer with a value list")
    return list(body["value"])
