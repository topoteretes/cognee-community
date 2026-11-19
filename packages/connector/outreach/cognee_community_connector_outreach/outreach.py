"""DLT source for Outreach (sequences, prospects, sequence memberships, activity).

Ingests Outreach records as small Markdown documents so they flow through cognee's
cognify pipeline like the other document connectors (``cognee_document_source =
"outreach"``). Prospect data is personal data, so everything is allowlisted and contact
details / message bodies are opt-in (see "Privacy").

Design
------
* **HTTP** — every list endpoint is configured through dlt's declarative ``rest_api``
  source (endpoint, auth, params, ``links.next`` pagination). Single calls (startup check,
  token refresh) use dlt's ``RESTClient`` / retrying session. All sessions retry 429/5xx with
  backoff (``Retry-After`` honoured) behind a client-side throttle. No hand-rolled loops.
* **Auth** — OAuth 2.0 authorization-code. ``outreach-auth`` (``auth_main``) performs the
  one-time browser consent and writes a 0600 token store; ``OutreachAuth`` then refreshes
  proactively before expiry and once on a 401, persisting any rotated refresh token
  atomically *before* the new access token is used.
* **Incremental** — per stream, ``cursor_updated_at`` lives in dlt resource state. A run asks
  for ``filter[updatedAt]=<cursor - overlap>..inf`` sorted ascending; ``merge`` on ``id``
  absorbs the overlap. The cursor only moves when the generator ran to completion (dlt
  commits state together with the load package).
* **Deletes** — the updatedAt filter cannot show deletions and the REST API has no delete
  feed, so every ``sweep_every_n_runs`` runs an id-only sweep lists the ids that still exist.
  ``known - sweep`` become ``_deleted`` tombstones (``merge`` + ``hard_delete``), which
  cognee's ``orphan_cleanup`` turns into graph/vector removal. Tombstones are emitted only
  after a *clean* sweep, never beyond ``max_delete_ratio`` (unless ``force_delete``).
* **Write disposition** — ``merge`` is required for incremental runs. cognee's default run
  disposition is ``replace``, under which every row that is not re-emitted is forgotten, so
  the source reads the *effective* disposition at run time and falls back to a full
  snapshot pass when it is not ``merge`` (``full_reconcile=True`` forces that too).

Privacy
-------
Only explicit per-type attribute allowlists are read; unknown attributes never reach a
document. Contact fields (emails, phones, addresses) need ``include_contact_details``;
message / call / task / meeting text needs ``include_activity_bodies``. Tokens and record
content are never logged — log lines carry counts and opaque ids only.

Every API detail not confirmed by Outreach's public docs is a named constant or parameter
below, marked ``ASSUMPTION``; ``examples/verify_live.py`` checks them against a real account.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import functools
import hashlib
import http.server
import json
import os
import random
import re
import secrets
import stat
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("outreach_connector")

# ===========================================================================
# Constants — every unverified API detail lives here (ASSUMPTION = not in public docs)
# ===========================================================================

OUTREACH_SOURCE_NAME = "outreach"
SKIPPED_TABLE = "outreach_skipped"

# Confirmed: https://developers.outreach.io/api/making-requests/
DEFAULT_BASE_URL = "https://api.outreach.io/api/v2"
# Confirmed: https://developers.outreach.io/api/oauth/
AUTHORIZE_URL = "https://api.outreach.io/oauth/authorize"
TOKEN_URL = "https://api.outreach.io/oauth/token"
JSONAPI_CONTENT_TYPE = "application/vnd.api+json"

CLIENT_ID_ENV = "OUTREACH_CLIENT_ID"
CLIENT_SECRET_ENV = "OUTREACH_CLIENT_SECRET"
BASE_URL_ENV = "OUTREACH_BASE_URL"
TOKEN_STORE_ENV = "OUTREACH_TOKEN_STORE"
REDIRECT_URI_ENV = "OUTREACH_REDIRECT_URI"
DEFAULT_TOKEN_STORE = "~/.config/cognee/outreach_tokens.json"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"

# Refresh this long before the access token expires (docs: expires_in = 7200).
TOKEN_EXPIRY_SKEW_SECONDS = 60
# ASSUMPTION: some OAuth servers require redirect_uri on refresh; Outreach docs list only
# client_id / client_secret / refresh_token. Sent only when configured.
SEND_REDIRECT_URI_ON_REFRESH = False

# Docs: offset-pagination max limit is 1000; the cursor-pagination max is not stated.
# ASSUMPTION: 1000 also holds for page[size].
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100
# ASSUMPTION: docs give no rate limit. ~10,000 requests/hour => one request per 0.36s.
RATE_LIMIT_PER_HOUR = 10_000
MIN_REQUEST_INTERVAL = 3600 / RATE_LIMIT_PER_HOUR
MAX_ATTEMPTS = 5
REQUEST_TIMEOUT_SECONDS = 30

# Query parameter names (docs-confirmed unless marked).
PARAM_UPDATED_AT_FILTER = "filter[updatedAt]"
PARAM_SORT = "sort"
SORT_UPDATED_ASC = "updatedAt"
PARAM_PAGE_SIZE = "page[size]"
PARAM_COUNT = "count"
NEXT_LINK_PATH = "links.next"
# ASSUMPTION: list filters take comma-separated values; relationship filters use the
# ``filter[<relationship>][id]`` form.
PARAM_ID_FILTER = "filter[id]"
PARAM_SEQUENCE_FILTER = "filter[sequence][id]"
PARAM_SEQUENCE_STATE_FILTER = "filter[state]"
# ASSUMPTION: the sweep asks for ``updatedAt`` only (needed to prune the activity window).
SWEEP_FIELDS = ("updatedAt",)
# ASSUMPTION: relationship names may be listed in a sparse fieldset so that relationship ids
# still come back. If Outreach rejects them, set this False (ids are then absent).
SPARSE_FIELDSETS_INCLUDE_RELATIONSHIPS = True
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

ALL_RESOURCES = ("sequences", "prospects", "activity")
ALL_ACTIVITY_TYPES = ("mailings", "calls", "tasks", "meetings")
DEFAULT_ACTIVITY_SINCE_DAYS = 90
MAX_FIELD_CHARS = 5000


@dataclass(frozen=True)
class _Stream:
    """One Outreach list endpoint -> one dlt table."""

    name: str  # internal key, also the table suffix
    group: str  # user-facing resource: sequences | prospects | activity
    path: str  # ASSUMPTION: endpoint path
    api_type: str  # ASSUMPTION: JSON:API type name used in fields[<type>]
    label: str  # document heading noun
    scopes: tuple[str, ...]  # ASSUMPTION: OAuth scopes needed to read it
    attributes: tuple[str, ...]  # allowlist (API camelCase names)
    contact: tuple[str, ...] = ()  # only with include_contact_details
    bodies: tuple[str, ...] = ()  # only with include_activity_bodies
    relationships: tuple[str, ...] = ()
    windowed: bool = False  # activity: bounded by activity_since

    @property
    def table(self) -> str:
        return f"outreach_{self.name}"


# ASSUMPTION (all attribute / relationship / scope names below): taken from Outreach's public
# object reference as remembered, not confirmed against a live account. Correcting one is a
# one-line change here.
STREAMS: dict[str, _Stream] = {
    s.name: s
    for s in (
        _Stream(
            "sequences",
            "sequences",
            "/sequences",
            "sequence",
            "sequence",
            ("sequences.read",),
            (
                "name",
                "description",
                "sequenceType",
                "shareType",
                "enabled",
                "locked",
                "tags",
                "durationInDays",
                "scheduleCount",
                "openCount",
                "clickCount",
                "replyCount",
                "bounceCount",
                "optOutCount",
                "createdAt",
                "updatedAt",
            ),
            relationships=("owner",),
        ),
        _Stream(
            "prospects",
            "prospects",
            "/prospects",
            "prospect",
            "prospect",
            ("prospects.read",),
            (
                "firstName",
                "lastName",
                "title",
                "occupation",
                "company",
                "tags",
                "engagedAt",
                "engagedScore",
                "openCount",
                "clickCount",
                "replyCount",
                "createdAt",
                "updatedAt",
            ),
            contact=(
                "emails",
                "workPhones",
                "homePhones",
                "mobilePhones",
                "otherPhones",
                "addressStreet",
                "addressStreet2",
                "addressCity",
                "addressState",
                "addressZip",
                "addressCountry",
            ),
            relationships=("account", "owner", "stage"),
        ),
        _Stream(
            "sequence_states",
            "prospects",
            "/sequenceStates",
            "sequenceState",
            "sequence membership",
            ("sequenceStates.read",),
            ("state", "stateChangedAt", "activeAt", "pauseReason", "createdAt", "updatedAt"),
            relationships=("prospect", "sequence", "sequenceStep"),
        ),
        _Stream(
            "mailings",
            "activity",
            "/mailings",
            "mailing",
            "email",
            ("mailings.read",),
            (
                "state",
                "mailingType",
                "scheduledAt",
                "deliveredAt",
                "openedAt",
                "clickedAt",
                "repliedAt",
                "bouncedAt",
                "openCount",
                "clickCount",
                "createdAt",
                "updatedAt",
            ),
            bodies=("subject", "bodyText"),
            relationships=("prospect", "sequence", "user", "account"),
            windowed=True,
        ),
        _Stream(
            "calls",
            "activity",
            "/calls",
            "call",
            "call",
            ("calls.read",),
            (
                "direction",
                "outcome",
                "state",
                "answeredAt",
                "completedAt",
                "createdAt",
                "updatedAt",
            ),
            contact=("to", "from"),
            bodies=("note",),
            relationships=("prospect", "sequence", "user", "account"),
            windowed=True,
        ),
        _Stream(
            "tasks",
            "activity",
            "/tasks",
            "task",
            "task",
            ("tasks.read",),
            ("action", "taskType", "state", "dueAt", "completedAt", "createdAt", "updatedAt"),
            bodies=("note",),
            relationships=("prospect", "sequence", "owner", "account"),
            windowed=True,
        ),
        _Stream(
            "meetings",
            "activity",
            "/meetings",
            "meeting",
            "meeting",
            ("meetings.read",),
            ("state", "startsAt", "endsAt", "duration", "createdAt", "updatedAt"),
            bodies=("title", "description"),
            relationships=("prospect", "sequence", "user", "account"),
            windowed=True,
        ),
    )
}

ALL_SCOPES = tuple(sorted({scope for s in STREAMS.values() for scope in s.scopes}))


# ===========================================================================
# Errors
# ===========================================================================


class OutreachError(Exception):
    """Base error. Messages never contain tokens or record content."""


class OutreachAuthError(OutreachError):
    """Credentials missing/rejected, or a scope is missing."""


class OutreachDeleteGuardError(OutreachError):
    """A sync would delete more than ``max_delete_ratio`` of the known records."""


class _BadRecord(Exception):  # noqa: N818 - internal control-flow signal
    """A single malformed record; carries a fixed-vocabulary reason (never content)."""

    def __init__(self, reason: str, record_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.record_id = record_id


# ===========================================================================
# Token store (0600 file, atomic writes, cross-thread and cross-process lock)
# ===========================================================================

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def resolve_token_store_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.environ.get(TOKEN_STORE_ENV) or DEFAULT_TOKEN_STORE).expanduser()


class TokenStore:
    """``{access_token, refresh_token, expires_at, scope}`` in a private JSON file."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = resolve_token_store_path(path)

    @property
    def _thread_lock(self) -> threading.RLock:
        # Looked up, not stored: dlt deep-copies the auth object, and locks cannot be copied.
        with _THREAD_LOCKS_GUARD:
            return _THREAD_LOCKS.setdefault(str(self.path), threading.RLock())

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Serialise refreshes across threads and (where ``fcntl`` exists) processes."""
        with self._thread_lock:
            handle = None
            try:
                import fcntl

                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(f"{self.path}.lock", "a")  # noqa: SIM115 - closed in finally
                fcntl.flock(handle, fcntl.LOCK_EX)
            except (ImportError, OSError):
                handle = None
            try:
                yield
            finally:
                if handle is not None:
                    handle.close()  # releases the flock

    def load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise OutreachAuthError(
                f"Token store {self.path} is unreadable ({type(exc).__name__}). "
                "Re-run `outreach-auth`."
            ) from None
        return data if isinstance(data, dict) else None

    def save(self, tokens: dict[str, Any]) -> None:
        """Atomic write: temp file (0600) in the same directory, fsync, ``os.replace``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".outreach_tokens_")
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w") as handle:
                json.dump(tokens, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# ===========================================================================
# OAuth: token exchange / refresh + auth class for dlt
# ===========================================================================


def _build_token_session():
    """Token-endpoint session: one attempt only.

    A retried refresh after the server already rotated the refresh token would present the
    dead old one and lock the account out; a failed attempt is simply retried next run.
    """
    return _build_session(max_attempts=1)


def _build_session(
    *,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_factor: float = 1.0,
    max_delay: float = 60.0,
    min_interval: float = MIN_REQUEST_INTERVAL,
    jitter: float = 0.05,
):
    """dlt retrying session (429/5xx, exponential backoff, Retry-After) + throttle.

    ``raise_for_status`` is off: callers inspect status codes. ``jitter`` randomises the
    throttle spacing so parallel runs don't synchronise.
    """
    from dlt.sources.helpers import requests as dlt_requests
    from requests.adapters import HTTPAdapter

    class _ThrottlingAdapter(HTTPAdapter):
        def __init__(self) -> None:
            super().__init__(pool_maxsize=10)
            self._lock = threading.Lock()
            self._ready_at = 0.0

        def send(self, request, **kwargs):
            with self._lock:
                wait = self._ready_at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._ready_at = time.monotonic() + min_interval + random.uniform(0, jitter)
            return super().send(request, **kwargs)

    client = dlt_requests.Client(
        raise_for_status=False,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
        request_max_attempts=max_attempts,
        request_backoff_factor=backoff_factor,
        request_max_retry_delay=max_delay,
    )
    session = client.session
    adapter = _ThrottlingAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _token_request(
    session: Any, token_url: str, data: dict[str, str], *, action: str
) -> dict[str, Any]:
    """POST to the token endpoint; returns the parsed tokens or raises ``OutreachAuthError``."""
    from dlt.sources.helpers import requests as dlt_requests

    try:
        response = session.post(token_url, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
    except (dlt_requests.RequestException, dlt_requests.RetryError) as exc:
        raise OutreachError(f"Outreach token endpoint unreachable during {action}.") from exc
    if response.status_code in (400, 401):
        raise OutreachAuthError(
            f"Outreach rejected the {action} (HTTP {response.status_code}): the refresh token "
            "or client credentials are invalid or revoked. Re-run `outreach-auth`."
        )
    if response.status_code >= 400:
        raise OutreachError(f"Outreach token endpoint returned HTTP {response.status_code}.")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not body.get("access_token"):
        raise OutreachError("Outreach token endpoint returned an unexpected response.")
    return body


def _tokens_from_response(body: dict[str, Any], previous: dict[str, Any] | None, now: float):
    expires_in = body.get("expires_in")
    expires_in = float(expires_in) if isinstance(expires_in, int | float | str) else 7200.0
    return {
        "access_token": body["access_token"],
        # Rotation: a new refresh token replaces the old one; absent => keep the old one.
        "refresh_token": body.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": now + expires_in,
        "scope": body.get("scope") or (previous or {}).get("scope"),
    }


@functools.lru_cache(maxsize=1)
def _auth_base():
    from dlt.common.configuration import configspec
    from dlt.sources.helpers.rest_client.auth import AuthConfigBase

    @configspec
    class OutreachAuth(AuthConfigBase):
        """Bearer auth with proactive refresh, one retry on 401 and durable rotation.

        Not credentials-from-config: build it with ``OutreachAuth.create(...)``.
        """

        def _init(
            self,
            *,
            client_id: str,
            client_secret: str,
            store: TokenStore,
            session_factory: Any,
            token_url: str = TOKEN_URL,
            redirect_uri: str | None = None,
            clock: Any = time.time,
        ) -> OutreachAuth:
            self._client_id, self._client_secret = client_id, client_secret
            self._store, self._session_factory, self._token_url = store, session_factory, token_url
            self._redirect_uri, self._clock = redirect_uri, clock
            self._tokens: dict[str, Any] | None = None
            self.refresh_count = 0
            return self

        # -- token lifecycle ---------------------------------------------------------

        def _fresh(self, tokens: dict[str, Any] | None) -> bool:
            return bool(
                tokens
                and tokens.get("access_token")
                and float(tokens.get("expires_at") or 0) > self._clock() + TOKEN_EXPIRY_SKEW_SECONDS
            )

        def access_token(self, *, force_refresh: bool = False, rejected: str | None = None) -> str:
            """A valid access token, refreshing under the store lock when needed.

            ``rejected`` is the token a server just 401-ed: another process may already have
            replaced it, in which case the stored one is used without another refresh.
            """
            if not force_refresh and self._fresh(self._tokens):
                return str(self._tokens["access_token"])  # type: ignore[index]
            with self._store.lock():
                stored = self._store.load()
                if self._fresh(stored) and (
                    not force_refresh or (rejected and stored["access_token"] != rejected)  # type: ignore[index]
                ):
                    self._tokens = stored
                    return str(stored["access_token"])  # type: ignore[index]
                if not stored or not stored.get("refresh_token"):
                    raise OutreachAuthError(
                        f"No Outreach refresh token in {self._store.path}. Run `outreach-auth`."
                    )
                self._tokens = self._refresh(stored)
                return str(self._tokens["access_token"])

        def _refresh(self, stored: dict[str, Any]) -> dict[str, Any]:
            data = {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": stored["refresh_token"],
            }
            if SEND_REDIRECT_URI_ON_REFRESH and self._redirect_uri:
                data["redirect_uri"] = self._redirect_uri
            body = _token_request(
                self._session_factory(), self._token_url, data, action="token refresh"
            )
            tokens = _tokens_from_response(body, stored, self._clock())
            # Durably persist the (possibly rotated) refresh token BEFORE the new access
            # token is handed to any request: a crash after this line loses nothing.
            self._store.save(tokens)
            self.refresh_count += 1
            logger.info("Outreach: access token refreshed.")
            return tokens

        # -- requests integration ----------------------------------------------------

        def __call__(self, request):
            token = self.access_token()
            request.headers["Authorization"] = f"Bearer {token}"
            request.register_hook("response", self._make_401_hook(token))
            return request

        def _make_401_hook(self, used_token: str):
            def hook(response, **kwargs):
                if response.status_code != 401:
                    return response
                token = self.access_token(force_refresh=True, rejected=used_token)
                retry = response.request.copy()
                retry.headers["Authorization"] = f"Bearer {token}"
                retry.deregister_hook("response", hook)  # at most one retry per request
                response.content  # noqa: B018 - drain so the connection can be reused
                response.close()
                return response.connection.send(retry, **kwargs)

            return hook

        @classmethod
        def create(cls, **kwargs: Any) -> OutreachAuth:
            return cls()._init(**kwargs)

    return OutreachAuth


def build_auth(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_store: str | os.PathLike[str] | None = None,
    token_url: str = TOKEN_URL,
    redirect_uri: str | None = None,
    session: Any = None,
    clock: Any = time.time,
):
    """Credentials from arguments, env vars, then dlt secrets (never logged)."""
    client_id = client_id or os.environ.get(CLIENT_ID_ENV) or _dlt_secret("client_id")
    client_secret = (
        client_secret or os.environ.get(CLIENT_SECRET_ENV) or _dlt_secret("client_secret")
    )
    if not client_id or not client_secret:
        raise OutreachAuthError(
            f"Outreach OAuth client credentials required: pass client_id/client_secret, set "
            f"{CLIENT_ID_ENV}/{CLIENT_SECRET_ENV}, or set sources.outreach.client_id / "
            "client_secret in dlt secrets."
        )
    return _auth_base().create(
        client_id=client_id,
        client_secret=client_secret,
        store=TokenStore(token_store),
        session_factory=(lambda: session) if session is not None else _build_token_session,
        token_url=token_url,
        redirect_uri=redirect_uri or os.environ.get(REDIRECT_URI_ENV),
        clock=clock,
    )


def _dlt_secret(key: str) -> str | None:
    try:
        import dlt

        value = dlt.secrets.get(f"sources.{OUTREACH_SOURCE_NAME}.{key}")
    except Exception:  # secrets provider unavailable / key missing
        return None
    return str(value) if value else None


# ---------------------------------------------------------------------------
# `outreach-auth`: one-time authorization-code flow with a loopback redirect
# ---------------------------------------------------------------------------


def build_authorize_url(
    client_id: str, redirect_uri: str, scopes: Sequence[str], state: str, base: str = AUTHORIZE_URL
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
        }
    )
    return f"{base}?{query}"


def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    store: TokenStore,
    session: Any = None,
    token_url: str = TOKEN_URL,
    clock: Any = time.time,
) -> dict[str, Any]:
    """Swap the authorization code for tokens and save them (0600). Returns the record."""
    body = _token_request(
        session or _build_token_session(),
        token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        action="authorization-code exchange",
    )
    tokens = _tokens_from_response(body, None, clock())
    if not tokens["refresh_token"]:
        raise OutreachError("Outreach did not return a refresh token; cannot sync unattended.")
    with store.lock():
        store.save(tokens)
    return tokens


def wait_for_code(redirect_uri: str, state: str, timeout: float = 300.0) -> str:
    """Serve one loopback request and return the authorization code (state-checked)."""
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        raise OutreachError("The redirect URI must be a loopback address (localhost).")
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = query.get("state", [""])[0] == state and bool(query.get("code"))
            if ok:
                result["code"] = query["code"][0]
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["error"] = "invalid_state"
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(
                b"Outreach authorization complete. You can close this tab."
                if ok
                else b"Outreach authorization failed. Return to the terminal."
            )

        def log_message(self, *args):  # silence: URLs carry the code
            pass

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    server.timeout = timeout
    try:
        server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        raise OutreachAuthError(
            f"Authorization failed ({result.get('error', 'timed out waiting for the redirect')})."
        )
    return result["code"]


def auth_main(argv: Sequence[str] | None = None) -> int:
    """Console script ``outreach-auth``."""
    parser = argparse.ArgumentParser(
        prog="outreach-auth", description="Authorize cognee to read your Outreach account."
    )
    parser.add_argument("--client-id", default=os.environ.get(CLIENT_ID_ENV))
    parser.add_argument("--client-secret", default=os.environ.get(CLIENT_SECRET_ENV))
    parser.add_argument(
        "--redirect-uri", default=os.environ.get(REDIRECT_URI_ENV) or DEFAULT_REDIRECT_URI
    )
    parser.add_argument("--scopes", nargs="+", default=list(ALL_SCOPES))
    parser.add_argument("--token-store", default=None, help=f"default: {DEFAULT_TOKEN_STORE}")
    parser.add_argument("--no-browser", action="store_true", help="print the URL only")
    args = parser.parse_args(argv)
    if not args.client_id or not args.client_secret:
        parser.error(
            f"--client-id/--client-secret (or {CLIENT_ID_ENV}/{CLIENT_SECRET_ENV}) required"
        )

    state = secrets.token_urlsafe(24)
    url = build_authorize_url(args.client_id, args.redirect_uri, args.scopes, state)
    print(
        f"Open this URL to authorize (redirect URI must be registered: {args.redirect_uri}):\n{url}"
    )
    if not args.no_browser:
        webbrowser.open(url)
    try:
        code = wait_for_code(args.redirect_uri, state)
        store = TokenStore(args.token_store)
        exchange_code(
            code,
            client_id=args.client_id,
            client_secret=args.client_secret,
            redirect_uri=args.redirect_uri,
            store=store,
        )
    except OutreachError as exc:
        print(f"error: {exc}")
        return 1
    print(f"Tokens saved to {store.path} (mode 0600).")
    return 0


# ===========================================================================
# Configuration
# ===========================================================================


@dataclass(frozen=True)
class _Config:
    resources: tuple[str, ...]
    sequence_ids: tuple[str, ...]
    sequence_states: tuple[str, ...]
    activity_types: tuple[str, ...]
    activity_since: timedelta
    include_contact_details: bool
    include_activity_bodies: bool
    page_size: int
    overlap_seconds: int
    sweep_every_n_runs: int
    full_reconcile: bool
    max_delete_ratio: float
    force_delete: bool
    skipped_table: bool

    def streams(self) -> list[_Stream]:
        chosen: list[_Stream] = []
        for stream in STREAMS.values():
            if stream.group not in self.resources:
                continue
            if stream.group == "activity" and stream.name not in self.activity_types:
                continue
            chosen.append(stream)
        return chosen


# ===========================================================================
# JSON:API flattening (pure)
# ===========================================================================

_SNAKE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")


def to_snake(name: str) -> str:
    return _SNAKE_2.sub(r"\1_\2", _SNAKE_1.sub(r"\1_\2", name)).lower()


def parse_ts(value: Any) -> datetime | None:
    """Timezone-aware datetime from an ISO-8601 string (naive input is taken as UTC)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime(TIMESTAMP_FORMAT)[:-4] + "Z"  # millisecond precision


def flatten_resource(resource: Any) -> dict[str, Any]:
    """Flatten one JSON:API resource object into a plain dict.

    ``id``/``type``; every attribute under its snake_case name; to-one relationships as
    ``<name>_id`` and to-many as ``<name>_ids`` (taken from the inline ``data``, never fetched);
    ``created_at`` / ``updated_at`` normalised to timezone-aware ISO strings; ``url`` from
    ``links.self``. Raises ``_BadRecord`` for a malformed resource.
    """
    if not isinstance(resource, dict):
        raise _BadRecord("not_an_object")
    raw_id = resource.get("id")
    if raw_id is None or isinstance(raw_id, bool | dict | list) or str(raw_id).strip() == "":
        raise _BadRecord("missing_id")
    rid = str(raw_id)
    attributes = resource.get("attributes")
    if attributes is None:
        attributes = {}
    if not isinstance(attributes, dict):
        raise _BadRecord("invalid_attributes", rid)

    flat: dict[str, Any] = {to_snake(key): value for key, value in attributes.items()}

    relationships = resource.get("relationships")
    for name, rel in (relationships if isinstance(relationships, dict) else {}).items():
        data = rel.get("data") if isinstance(rel, dict) else None
        key = to_snake(name)
        if isinstance(data, dict) and data.get("id") is not None:
            flat[f"{key}_id"] = str(data["id"])
        elif isinstance(data, list):
            flat[f"{key}_ids"] = [str(d["id"]) for d in data if isinstance(d, dict) and "id" in d]

    for key in ("created_at", "updated_at"):
        if flat.get(key) is None:
            continue
        parsed = parse_ts(flat[key])
        if parsed is None:
            raise _BadRecord(f"invalid_timestamp:{key}", rid)
        flat[key] = parsed.isoformat()

    links = resource.get("links")
    if isinstance(links, dict) and isinstance(links.get("self"), str):
        flat["url"] = links["self"]
    flat["id"] = rid
    flat["type"] = str(resource.get("type") or "")
    return flat


# ===========================================================================
# Normalisation: allowlist -> row (document + columns)
# ===========================================================================


def doc_id(stream: _Stream, record_id: str) -> str:
    return f"outreach:{to_snake(stream.api_type)}:{record_id}"


def allowed_attributes(stream: _Stream, cfg: _Config) -> tuple[str, ...]:
    names = list(stream.attributes)
    if cfg.include_contact_details:
        names += stream.contact
    if cfg.include_activity_bodies:
        names += stream.bodies
    return tuple(names)


def _fmt(value: Any) -> str:
    if isinstance(value, list) and all(not isinstance(v, dict | list) for v in value):
        text = ", ".join(str(v) for v in value)
    elif isinstance(value, dict | list):
        text = json.dumps(value, sort_keys=True, default=str)
    else:
        text = str(value)
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + "…"


def _title(stream: _Stream, fields: dict[str, Any], rid: str) -> str:
    if stream.name == "prospects":
        name = " ".join(str(fields[k]) for k in ("first_name", "last_name") if fields.get(k))
        label = name or rid
    elif stream.name == "sequence_states":
        prospect, sequence = fields.get("prospect_id", "?"), fields.get("sequence_id", "?")
        label = f"prospect {prospect} in sequence {sequence}"
    elif stream.name == "sequences":
        label = str(fields.get("name") or rid)
    else:
        label = str(fields.get("subject") or fields.get("title") or rid)
    return f"Outreach {stream.label}: {label}"


def normalise(stream: _Stream, resource: Any, cfg: _Config) -> dict[str, Any]:
    """One API resource -> one document row. Only allowlisted attributes are ever read."""
    flat = flatten_resource(resource)
    rid = flat["id"]
    names = [to_snake(a) for a in allowed_attributes(stream, cfg)]
    fields = {k: flat[k] for k in names if flat.get(k) is not None}
    relations: dict[str, Any] = {}
    for rel in stream.relationships:
        key = to_snake(rel)
        if f"{key}_id" in flat:
            relations[f"{key}_id"] = flat[f"{key}_id"]
        elif f"{key}_ids" in flat:
            relations[f"{key}_ids"] = ",".join(flat[f"{key}_ids"])  # no list => no child tables
    fields_for_title = {**fields, **relations}

    heading = _title(stream, fields_for_title, rid)
    lines = [f"- Record: {stream.label} {doc_id(stream, rid)}"]
    lines += [
        f"- {k.replace('_', ' ').capitalize()}: {_fmt(v)}"
        for k, v in {**fields, **relations}.items()
        if v not in (None, "", [])
    ]
    return {
        "id": doc_id(stream, rid),
        "source_id": rid,
        "record_type": to_snake(stream.api_type),
        "title": heading,
        "content": "\n".join(lines),
        "url": flat.get("url"),
        "updated_at": flat.get("updated_at"),
        **relations,
        "_deleted": False,
    }


# ===========================================================================
# Compact state: known ids (with updated-at epoch)
# ===========================================================================


def pack_known(known: dict[str, int]) -> str:
    ids = sorted(known)
    payload = json.dumps({"i": ids, "e": [known[i] for i in ids]}, separators=(",", ":"))
    return base64.b64encode(zlib.compress(payload.encode())).decode()


def unpack_known(packed: Any) -> dict[str, int]:
    if not packed:
        return {}
    try:
        data = json.loads(zlib.decompress(base64.b64decode(packed)))
        return {str(i): int(e) for i, e in zip(data["i"], data["e"], strict=True)}
    except (ValueError, KeyError, TypeError, zlib.error):
        logger.warning("Outreach: stored id state is unreadable; starting a fresh snapshot.")
        return {}


# ===========================================================================
# HTTP layer: dlt rest_api + guarded link paginator
# ===========================================================================


def _paginator(base_url: str):
    from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

    host = urllib.parse.urlparse(base_url).netloc

    class _GuardedLinkPaginator(JSONLinkPaginator):
        """Follows ``links.next``; stops on empty, repeated, looping or cross-host pages.

        A next link pointing at another host would carry the bearer token there, so it is
        treated like a pagination fault (``repeated``): the pass is incomplete.
        """

        def __init__(self, max_pages: int = 1_000_000) -> None:
            super().__init__(next_url_path=NEXT_LINK_PATH)
            self.max_pages = max_pages
            self.repeated = False
            self._pages = 0
            self._prev_signature: str | None = None
            self._seen_links: set[str] = set()

        def update_state(self, response, data=None) -> None:
            super().update_state(response, data)
            self._pages += 1
            if not data:
                self._has_next_page = False
                return
            signature = hashlib.sha256(
                "|".join(str(r.get("id")) if isinstance(r, dict) else "?" for r in data).encode()
            ).hexdigest()
            link = self._next_reference
            if link and urllib.parse.urlparse(link).netloc not in ("", host):
                self.repeated = True
                self._has_next_page = False
                return
            if (
                signature == self._prev_signature
                or self._pages >= self.max_pages
                or (link and link in self._seen_links)
            ):
                self.repeated = True
                self._has_next_page = False
                return
            self._prev_signature = signature
            if link:
                self._seen_links.add(link)

    return _GuardedLinkPaginator()


class _Api:
    """Thin Outreach client over dlt's RESTClient / rest_api source."""

    def __init__(self, base_url: str, auth: Any, session: Any):
        from dlt.sources.helpers.rest_client import RESTClient

        self._base_url, self._auth, self._session = base_url, auth, session
        self._headers = {"Accept": JSONAPI_CONTENT_TYPE, "Content-Type": JSONAPI_CONTENT_TYPE}
        self._client = RESTClient(
            base_url=base_url, auth=auth, session=session, headers=self._headers
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    def probe(self, path: str) -> int:
        """One cheap authenticated GET; returns the HTTP status."""
        params = {PARAM_PAGE_SIZE: 1, PARAM_COUNT: "false"}
        return self._client.get(path, params=params).status_code

    def records(self, path: str, params: dict[str, Any], paginator: Any) -> Iterator[Any]:
        from dlt.sources.rest_api import rest_api_resources

        config = {
            "client": {
                "base_url": self._base_url,
                "auth": self._auth,
                "session": self._session,
                "headers": self._headers,
            },
            "resources": [
                {
                    "name": "outreach_list",
                    "endpoint": {
                        "path": path,
                        "params": params,
                        "data_selector": "data",
                        "paginator": paginator,
                    },
                }
            ],
        }
        yield from rest_api_resources(config)[0]


def _causes(exc: BaseException) -> list[BaseException]:
    """The exception and its cause/context chain (dlt wraps errors raised inside resources)."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _auth_error(status: int, stream: _Stream | None = None) -> OutreachAuthError:
    if status == 401:
        return OutreachAuthError(
            "Outreach rejected the access token (HTTP 401) even after a refresh. "
            "Re-run `outreach-auth`, and check the client id/secret and base URL."
        )
    scopes = f" ({', '.join(stream.scopes)} for {stream.name})" if stream else ""
    return OutreachAuthError(
        f"Outreach denied access (HTTP 403). The OAuth app is missing a scope{scopes}; "
        "enable it for the app and re-run `outreach-auth`."
    )


class _Pass:
    """Iterates one list endpoint and records whether the sweep was complete.

    ``strict`` passes (the data pass) raise on any failure; non-strict passes (the delete
    sweep) mark themselves incomplete on transient and auth/scope errors so that no
    tombstones are derived from them.
    """

    def __init__(self, api: _Api, stream: _Stream, params: dict[str, Any], *, strict: bool):
        self._api, self._stream, self._params, self._strict = api, stream, params, strict
        self._paginator = _paginator(api.base_url)
        self.fetched = 0
        self.complete = True
        self.reason: str | None = None

    def mark_incomplete(self, reason: str) -> None:
        if self.complete:
            self.complete, self.reason = False, reason

    def _fail(self, reason: str, error: OutreachError) -> None:
        if self._strict:
            raise error
        self.mark_incomplete(reason)

    def records(self) -> Iterator[Any]:
        from dlt.sources.helpers import requests as dlt_requests

        try:
            for record in self._api.records(self._stream.path, self._params, self._paginator):
                self.fetched += 1
                yield record
        except OutreachError:
            raise
        except Exception as exc:
            chain = _causes(exc)
            auth = next((e for e in chain if isinstance(e, OutreachError)), None)
            http = next((e for e in chain if isinstance(e, dlt_requests.HTTPError)), None)
            if auth is not None:
                self._fail("auth_error", auth)
            elif http is not None:
                status = http.response.status_code if http.response is not None else 0
                if status in (401, 403):
                    self._fail(f"http_{status}", _auth_error(status, self._stream))
                elif status == 429 or status >= 500:
                    self._fail(
                        f"http_{status}",
                        OutreachError(
                            f"Outreach {self._stream.path}: HTTP {status} after retries."
                        ),
                    )
                else:
                    raise OutreachError(
                        f"Outreach request to {self._stream.path} failed with HTTP {status}."
                    ) from None
            elif any(
                isinstance(e, dlt_requests.RequestException | dlt_requests.RetryError)
                for e in chain
            ):
                self._fail(
                    "network_error", OutreachError(f"Outreach {self._stream.path}: network error.")
                )
            elif any(isinstance(e, ValueError) for e in chain):  # non-JSON body
                self._fail(
                    "invalid_response",
                    OutreachError(f"Outreach {self._stream.path}: invalid response body."),
                )
            else:
                raise
        else:
            if self._paginator.repeated:
                self._fail(
                    "repeated_page",
                    OutreachError(f"Outreach {self._stream.path}: pagination repeated a page."),
                )


# ===========================================================================
# Sync: data pass -> rows, optional id sweep -> tombstones, state commit
# ===========================================================================


@dataclass
class _Skips:
    """Skipped records: ids and fixed-vocabulary reasons only, never content."""

    items: list[tuple[str, str | None, str]] = field(default_factory=list)

    def add(self, kind: str, record_id: str | None, reason: str) -> None:
        self.items.append((kind, record_id, reason))
        logger.warning("Outreach %s: skipped record %s (%s).", kind, record_id or "<no id>", reason)


def list_params(stream: _Stream, cfg: _Config, floor: datetime | None, *, sweep: bool):
    params: dict[str, Any] = {PARAM_PAGE_SIZE: cfg.page_size, PARAM_COUNT: "false"}
    if sweep:
        fields = list(SWEEP_FIELDS)
    else:
        params[PARAM_SORT] = SORT_UPDATED_ASC
        fields = list(allowed_attributes(stream, cfg))
        if SPARSE_FIELDSETS_INCLUDE_RELATIONSHIPS:
            fields += list(stream.relationships)
    params[f"fields[{stream.api_type}]"] = ",".join(fields)
    if floor is not None:
        params[PARAM_UPDATED_AT_FILTER] = f"{format_ts(floor)}..inf"
    if stream.name == "sequences" and cfg.sequence_ids:
        params[PARAM_ID_FILTER] = ",".join(cfg.sequence_ids)
    if stream.name == "sequence_states":
        if cfg.sequence_ids:
            params[PARAM_SEQUENCE_FILTER] = ",".join(cfg.sequence_ids)
        if cfg.sequence_states:
            params[PARAM_SEQUENCE_STATE_FILTER] = ",".join(cfg.sequence_states)
    return params


def _epoch(value: Any) -> int | None:
    parsed = parse_ts(value)
    return int(parsed.timestamp()) if parsed else None


def _sweep_ids(sweep: _Pass) -> dict[str, int]:
    found: dict[str, int] = {}
    for record in sweep.records():
        raw = record.get("id") if isinstance(record, dict) else None
        if raw is None or isinstance(raw, bool | dict | list) or str(raw).strip() == "":
            sweep.mark_incomplete("record_without_id")
            continue
        attrs = record.get("attributes")
        updated = attrs.get("updatedAt") if isinstance(attrs, dict) else None
        found[str(raw)] = _epoch(updated) or 0
    return found


def sync_stream(
    stream: _Stream,
    api: _Api,
    cfg: _Config,
    state: dict[str, Any],
    disposition: str,
    skips: _Skips,
    now: datetime,
) -> Iterator[dict[str, Any]]:
    """Single run for one stream. State is mutated only at the very end, so an exception
    anywhere earlier leaves cursor, known ids and run counter untouched.
    """
    snapshot = cfg.full_reconcile or disposition != "merge"
    known = unpack_known(state.get("known"))
    cursor = parse_ts(state.get("cursor_updated_at"))
    window_floor = now - cfg.activity_since if stream.windowed else None

    if snapshot:
        floor = window_floor
    elif cursor is not None:
        floor = cursor - timedelta(seconds=cfg.overlap_seconds)
    else:
        floor = window_floor

    data = _Pass(api, stream, list_params(stream, cfg, floor, sweep=False), strict=True)
    touched: dict[str, int] = {}
    max_updated = cursor
    skipped_before = len(skips.items)
    emitted = 0
    for resource in data.records():
        try:
            row = normalise(stream, resource, cfg)
        except _BadRecord as exc:
            skips.add(stream.name, exc.record_id, exc.reason)
            if exc.record_id is not None:
                touched.setdefault(exc.record_id, 0)
            continue
        updated = parse_ts(row["updated_at"])
        touched[row["source_id"]] = int(updated.timestamp()) if updated else 0
        if updated:
            updated = min(updated, now)  # a clock-skewed future timestamp must not skip data
            if max_updated is None or updated > max_updated:
                max_updated = updated
        emitted += 1
        yield row

    known_after = {**known, **{i: e or known.get(i, 0) for i, e in touched.items()}}
    window_floor_epoch = int(window_floor.timestamp()) if window_floor else None
    if window_floor_epoch is not None:
        known_after = {i: e for i, e in known_after.items() if e >= window_floor_epoch}

    run_no = int(state.get("runs", 0)) + 1
    tombstones: list[str] = []
    sweep_status = "not_run"
    final_known = known_after

    if snapshot:
        sweep_status = "snapshot"
        final_known = (
            dict(touched)
            if not window_floor_epoch
            else {i: e for i, e in touched.items() if e >= window_floor_epoch}
        )
        if cfg.full_reconcile and disposition == "merge":
            # The full pass is itself a complete listing: derive deletions from it.
            tombstones = _guard(stream, sorted(set(known) - set(touched)), known, cfg)
            sweep_status = "full_reconcile"
    elif known and run_no % cfg.sweep_every_n_runs == 0:
        sweep = _Pass(api, stream, list_params(stream, cfg, window_floor, sweep=True), strict=False)
        found = _sweep_ids(sweep)
        if sweep.complete and not found and known_after and not cfg.force_delete:
            # Everything vanished at once looks like scope loss, not mass deletion.
            sweep.mark_incomplete("empty_response")
        if sweep.complete:
            tombstones = _guard(stream, sorted(set(known_after) - set(found)), known_after, cfg)
            sweep_status = "complete"
            final_known = {i: max(e, found[i]) for i, e in known_after.items() if i in found}
        else:
            sweep_status = f"incomplete:{sweep.reason}"
            logger.warning(
                "Outreach %s: delete sweep incomplete (%s); no deletions.",
                stream.name,
                sweep.reason,
            )

    for rid in tombstones:
        yield {"id": doc_id(stream, rid), "source_id": rid, "_deleted": True}

    # --- commit state (only reached when the generator ran to completion) ---------------
    state["known"] = pack_known(final_known)
    if max_updated is not None:
        state["cursor_updated_at"] = max_updated.isoformat()
    state["runs"] = run_no
    state["last_run"] = {
        "at": now.isoformat(),
        "fetched": data.fetched,
        "emitted": emitted,
        "skipped": len(skips.items) - skipped_before,
        "tombstoned": len(tombstones),
        "sweep": sweep_status,
        "mode": "snapshot" if snapshot else "incremental",
    }
    logger.info(
        "Outreach %s: fetched=%d emitted=%d skipped=%d tombstoned=%d sweep=%s mode=%s.",
        stream.name,
        data.fetched,
        emitted,
        state["last_run"]["skipped"],
        len(tombstones),
        sweep_status,
        state["last_run"]["mode"],
    )


def _guard(stream: _Stream, tombstones: list[str], known: dict[str, int], cfg: _Config):
    ratio = len(tombstones) / len(known) if known else 0.0
    if tombstones and ratio > cfg.max_delete_ratio and not cfg.force_delete:
        raise OutreachDeleteGuardError(
            f"Outreach {stream.name}: sync would delete {len(tombstones)} of {len(known)} known "
            f"records ({ratio:.0%}), above max_delete_ratio={cfg.max_delete_ratio:.0%}. "
            "Nothing was changed. If this is expected, re-run with force_delete=True."
        )
    return tombstones


# ===========================================================================
# Public factory
# ===========================================================================


def _effective_disposition(source_resources: Any, name: str, default: str = "replace") -> str:
    """Write disposition that will actually be applied (the caller's run-level override wins).

    If it cannot be determined, assume ``replace``: that selects the always-safe full snapshot.
    """
    try:
        value = source_resources[name].write_disposition
    except Exception:
        return default
    if isinstance(value, dict):
        value = value.get("disposition")
    return value or default


def _skipped_item(kind: str, record_id: str | None, reason: str, ordinal: int, now: datetime):
    import dlt

    discriminator = record_id or f"unknown-{reason}-{ordinal}"
    return dlt.mark.with_table_name(
        {
            "id": f"outreach:skipped:{kind}:{discriminator}",
            "record_type": kind,
            "source_id": record_id,
            "reason": reason,
            "title": f"Skipped Outreach {kind}",
            "content": f"Skipped Outreach {kind} {record_id or '(no id)'}: {reason}",
            "seen_at": now.isoformat(),
        },
        SKIPPED_TABLE,
    )


def _lookback(value: timedelta | int | float | None) -> timedelta:
    if value is None:
        return timedelta(days=DEFAULT_ACTIVITY_SINCE_DAYS)
    return value if isinstance(value, timedelta) else timedelta(days=float(value))


def outreach_source(
    resources: Sequence[str] = ALL_RESOURCES,
    sequence_ids: Sequence[str | int] | None = None,
    sequence_states: Sequence[str] | None = None,
    activity_types: Sequence[str] = ALL_ACTIVITY_TYPES,
    activity_since: timedelta | int | float | None = None,
    include_contact_details: bool = False,
    include_activity_bodies: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    overlap_seconds: int = 1,
    sweep_every_n_runs: int = 1,
    full_reconcile: bool = False,
    max_delete_ratio: float = 0.5,
    force_delete: bool = False,
    skipped_table: bool = False,
    base_url: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_store: str | os.PathLike[str] | None = None,
    redirect_uri: str | None = None,
    session: Any = None,
    auth: Any = None,
):
    """Create a dlt source that yields Outreach records as Markdown documents.

    Hand it to ``cognee.remember(source, write_disposition="merge", primary_key="id")``.
    ``merge`` makes sync incremental; if the caller leaves cognee's default ``replace`` the
    source falls back to a full snapshot pass, which is safe but sends every record each run.

    Args:
        resources: Subset of ``["sequences", "prospects", "activity"]`` (default all).
            ``prospects`` also loads sequence memberships (``/sequenceStates``).
        sequence_ids: Only these sequences / their memberships.
        sequence_states: Only memberships in these states (e.g. ``["active", "paused"]``).
        activity_types: Subset of ``["mailings", "calls", "tasks", "meetings"]``.
        activity_since: First-sync lookback for activity (``timedelta`` or days; default 90).
            It is also the retention window: older activity is never swept for deletion.
        include_contact_details: Also ingest emails, phones and postal addresses (default off).
        include_activity_bodies: Also ingest subjects / email text / call and task notes /
            meeting titles and descriptions (default off; metadata only).
        page_size: Records per request, capped at ``MAX_PAGE_SIZE``.
        overlap_seconds: Seconds subtracted from the stored cursor on each run.
        sweep_every_n_runs: Run the id-only delete sweep every N runs (default every run).
        full_reconcile: Emit every record each run (and derive deletes from that listing).
        max_delete_ratio / force_delete: Abort a sweep that would delete more than this
            fraction of known records, unless ``force_delete`` is set.
        skipped_table: Also load skipped-record ids/reasons into ``outreach_skipped`` (off by
            default because cognee ingests every table of a document source).
        base_url: API base (env ``OUTREACH_BASE_URL``; default ``DEFAULT_BASE_URL``).
        client_id / client_secret: OAuth app credentials (env ``OUTREACH_CLIENT_ID`` /
            ``OUTREACH_CLIENT_SECRET`` or dlt secrets ``sources.outreach.*``).
        token_store: Path of the token file written by ``outreach-auth``
            (env ``OUTREACH_TOKEN_STORE``).
        redirect_uri: Only needed if ``SEND_REDIRECT_URI_ON_REFRESH`` is enabled.
        session / auth: Pre-built ``requests`` session / auth object (test-injection points).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Outreach connector requires dlt: pip install "dlt[sqlalchemy]" (or cognee[dlt]).'
        ) from exc

    if not resources or set(resources) - set(ALL_RESOURCES):
        raise ValueError(f"resources must be a non-empty subset of {list(ALL_RESOURCES)}.")
    if set(activity_types) - set(ALL_ACTIVITY_TYPES) or (
        "activity" in resources and not activity_types
    ):
        raise ValueError(
            f"activity_types must be a non-empty subset of {list(ALL_ACTIVITY_TYPES)}."
        )
    if not 0 < max_delete_ratio <= 1:
        raise ValueError("max_delete_ratio must be in (0, 1].")
    if page_size < 1 or overlap_seconds < 0 or sweep_every_n_runs < 1:
        raise ValueError("page_size and sweep_every_n_runs must be >= 1; overlap_seconds >= 0.")
    if page_size > MAX_PAGE_SIZE:
        logger.warning("Outreach: page_size %d capped to %d.", page_size, MAX_PAGE_SIZE)

    cfg = _Config(
        resources=tuple(resources),
        sequence_ids=tuple(str(i) for i in sequence_ids or ()),
        sequence_states=tuple(sequence_states or ()),
        activity_types=tuple(activity_types),
        activity_since=_lookback(activity_since),
        include_contact_details=include_contact_details,
        include_activity_bodies=include_activity_bodies,
        page_size=min(page_size, MAX_PAGE_SIZE),
        overlap_seconds=overlap_seconds,
        sweep_every_n_runs=sweep_every_n_runs,
        full_reconcile=full_reconcile,
        max_delete_ratio=max_delete_ratio,
        force_delete=force_delete,
        skipped_table=skipped_table,
    )
    resolved_url = (base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
    shared_session = session
    runtime: dict[str, Any] = {"api": None, "validated": set()}
    skips = _Skips()

    def api_for(stream: _Stream) -> _Api:
        """Build the client lazily and validate credentials once per endpoint, before syncing."""
        if runtime["api"] is None:
            sess = shared_session or _build_session()
            the_auth = auth or build_auth(
                client_id=client_id,
                client_secret=client_secret,
                token_store=token_store,
                redirect_uri=redirect_uri,
            )
            runtime["api"] = _Api(resolved_url, the_auth, sess)
        if stream.name not in runtime["validated"]:
            status = runtime["api"].probe(stream.path)
            if status in (401, 403):
                raise _auth_error(status, stream)
            if status >= 400:
                raise OutreachError(
                    f"Outreach startup check on {stream.path} failed: HTTP {status}."
                )
            runtime["validated"].add(stream.name)
        return runtime["api"]

    def flush_skips(now: datetime):
        if cfg.skipped_table:
            ordinals: dict[tuple[str, str], int] = {}
            for kind, rid, reason in skips.items:
                ordinals[(kind, reason)] = ordinals.get((kind, reason), 0) + 1
                yield _skipped_item(kind, rid, reason, ordinals[(kind, reason)], now)
        skips.items.clear()

    hard_delete = {"_deleted": {"data_type": "bool", "hard_delete": True}}

    def make_resource(stream: _Stream):
        @dlt.resource(
            name=stream.table, primary_key="id", write_disposition="merge", columns=hard_delete
        )
        def _resource():
            api = api_for(stream)
            disposition = _effective_disposition(dlt.current.source().resources, stream.table)
            now = datetime.now(UTC)
            yield from sync_stream(
                stream, api, cfg, dlt.current.resource_state(), disposition, skips, now
            )
            yield from flush_skips(now)

        return _resource

    selected = [make_resource(stream)() for stream in cfg.streams()]

    @dlt.source(name=OUTREACH_SOURCE_NAME)
    def _outreach():
        return selected

    source = _outreach()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, OUTREACH_SOURCE_NAME)
    return source
