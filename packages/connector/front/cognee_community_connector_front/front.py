"""Front connector for cognee: a dlt resource that syncs selected inboxes into memory.

Each conversation (its messages and, optionally, its internal comments) becomes one
document, merged on the conversation id, and is meant to be handed to
``cognee.remember``::

    await cognee.remember(
        front_source(token="<API token>", inboxes=["inb_..."]),
        dataset_name="front",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )
"""

import copy
import hashlib
import html
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("front_connector")

FRONT_SOURCE_NAME = "front"
DEFAULT_API_URL = "https://api2.frontapp.com"
# Front ids are a type prefix and lowercase letters and digits; checked before one
# goes into a path.
_INBOX_RE = re.compile(r"^inb_[a-z0-9]+$")
_CONVERSATION_RE = re.compile(r"^cnv_[a-z0-9]+$")
# dlt snake-cases table names and collapses repeated underscores, so only names it
# leaves unchanged are accepted: single underscores between letters and digits.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_RESOURCE_NAME_MAX = 51
# The largest page Front returns.
_PAGE_SIZE = 100
# Attempts per request: the first try plus four retries on 429, 5xx and dropped
# connections.
_MAX_RETRIES = 5
# cognee holds a process-wide staging lock while a dlt source runs, so a longer wait
# is not slept through: the run stops cleanly and the next one resumes.
_MAX_RETRY_AFTER = 30.0
# When a response says this many requests or fewer are left in the rate-limit window,
# the client waits for the window to reset before the next request.
_PACE_BELOW = 2
# Runs in a row one inbox may fail (lost access, a 5xx) while the others still sync.
_FAILED_RUNS_BEFORE_RAISE = 3
# The longest title, taken from the subject.
_TITLE_MAX = 80
_COLUMNS = {
    "_deleted": {"data_type": "bool", "hard_delete": True},
    "title": {"data_type": "text", "nullable": True},
    "content": {"data_type": "text", "nullable": True},
    "url": {"data_type": "text", "nullable": True},
    "visibility": {"data_type": "text", "nullable": True},
    **dlt_utils.NODE_SET_COLUMN_HINT,
}


class FrontAPIError(RuntimeError):
    """A Front API call failed.

    ``status`` is the HTTP status when the server answered (``None`` otherwise), and
    ``location`` the target of a redirect. ``ends_run`` marks errors that would fail
    every inbox the same way.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        ends_run: bool = False,
        location: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.ends_run = ends_run
        self.location = location


class FrontAuthError(FrontAPIError):
    """Front rejected the token (HTTP 401)."""

    def __init__(self, message: str):
        super().__init__(message, 401, ends_run=True)


class FrontRateLimitError(FrontAPIError):
    """Still rate limited after the retries, or asked to wait longer than we sleep."""

    def __init__(self, message: str):
        super().__init__(message, 429)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A merged conversation answers 301 to the conversation it was merged into, and
    # following it would store the survivor's content under the old id. urllib would
    # also keep the Authorization header on a redirect to another host. So a redirect
    # is never followed: it surfaces as the 3xx HTTPError instead.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(path: str) -> str:
    """The request path with ids replaced, for errors and logs."""
    return re.sub(r"/[a-z]{2,4}_[A-Za-z0-9]+(?=/|$)", "/{id}", path.split("?")[0])


def _api_url(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("api_url must be a URL like https://api2.frontapp.com.")
    parsed = urllib.parse.urlparse(url.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "api_url must be a plain https URL like https://api2.frontapp.com, so the "
            "token never travels in clear text."
        )
    return f"https://{parsed.netloc.lower()}"


def _token(value: Any) -> str:
    """The token without surrounding whitespace, refused if it holds any inside.

    A pasted token often ends in a newline, and urllib quotes the whole header,
    token included, in the error it raises for one. The message never echoes it.
    """
    token = value.strip() if isinstance(value, str) else ""
    if not token or any(ch.isspace() or ord(ch) < 32 for ch in token):
        raise ValueError("Pass token=, a Front API token (no spaces).")
    return token


def _retry_after(headers) -> float | None:
    try:
        return max(0.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int) -> float:
    return float(min(2**attempt, 16))


# ---------------------------------------------------------------------------
# HTTP client (stdlib only)
# ---------------------------------------------------------------------------
class FrontClient:
    """Minimal Front Core API client: ``get(path, params=)`` returns the decoded JSON.

    Retries 429 (honouring ``Retry-After`` up to 30 s), 5xx and dropped connections,
    and raises :class:`FrontAPIError` otherwise. Requests only ever go to ``api_url``
    and redirects are not followed: a 3xx raises with ``status`` and ``location``.
    """

    def __init__(
        self,
        token: str,
        *,
        api_url: str = DEFAULT_API_URL,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        check_active: Callable[[], None] | None = None,
    ):
        self.api_url = _api_url(api_url)
        self._auth = f"Bearer {_token(token)}"
        self._timeout = timeout
        self._sleep = sleep
        self._check_active = check_active
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._clock = time.time

    def __repr__(self) -> str:
        # The token is a password: keep it out of any repr, log or traceback.
        return f"FrontClient(api_url={self.api_url!r}, token=<redacted>)"

    def _open(self, request):
        return self._opener.open(request, timeout=self._timeout)

    def _wait(self, seconds: float) -> None:
        # A host that revoked access must not wait through the retries.
        if self._check_active is not None:
            self._check_active()
        self._sleep(seconds)

    def _pace(self, headers) -> None:
        """Wait for the window to reset when the company's rate limit is nearly used up.

        Front's limit is per company and depends on the plan, so the connector reads
        ``x-ratelimit-remaining`` and ``x-ratelimit-reset`` rather than assuming a rate,
        and leaves room for the company's other integrations. A wait longer than 30 s
        is left to the 429 handling, which stops the run cleanly.
        """
        try:
            remaining = int(headers.get("x-ratelimit-remaining"))
            reset = float(headers.get("x-ratelimit-reset"))
        except (TypeError, ValueError):
            return
        if remaining > _PACE_BELOW:
            return
        wait = reset - self._clock()
        if 0 < wait <= _MAX_RETRY_AFTER:
            self._wait(wait)

    def get(self, path: str, params: dict | None = None) -> Any:
        if not path.startswith("/"):
            raise ValueError("Front paths start with '/'.")
        url = f"{self.api_url}{path}"
        if params:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        endpoint = _endpoint(path)
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(
                url, headers={"Authorization": self._auth, "Accept": "application/json"}
            )
            try:
                with self._open(request) as response:
                    raw = response.read()
                    self._pace(response.headers)
            except urllib.error.HTTPError as exc:
                # Every raise below uses ``from None``: the HTTPError carries the request
                # and its headers, and errors name the endpoint without ids.
                status = exc.code
                if status == 401:
                    raise FrontAuthError(
                        "Front rejected the token (HTTP 401). Check it was not revoked."
                    ) from None
                if 300 <= status < 400:
                    raise FrontAPIError(
                        f"Front redirected {endpoint} (HTTP {status}); it is not followed.",
                        status,
                        location=exc.headers.get("Location"),
                    ) from None
                if status == 429:
                    delay = _retry_after(exc.headers)
                    if delay is not None and delay > _MAX_RETRY_AFTER:
                        raise FrontRateLimitError(
                            f"Front asked to wait {delay:.0f}s on {endpoint}."
                        ) from None
                    if attempt == _MAX_RETRIES - 1:
                        raise FrontRateLimitError(
                            f"Front still rate limited {endpoint} after {_MAX_RETRIES} tries."
                        ) from None
                if (status == 429 or status >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = _retry_after(exc.headers) if status == 429 else None
                    delay = delay if delay is not None else _backoff(attempt)
                    logger.warning(
                        "Front: HTTP %s on %s, retrying in %.0fs (%d/%d).",
                        status,
                        endpoint,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    self._wait(delay)
                    continue
                raise FrontAPIError(
                    f"Front returned HTTP {status} for {endpoint}.", status
                ) from None
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts, ConnectionResetError, RemoteDisconnected and
                # IncompleteRead: a dropped connection is retried like a 5xx.
                if attempt < _MAX_RETRIES - 1:
                    self._wait(_backoff(attempt))
                    continue
                # ``from None``: the error's text can carry the request URL.
                raise FrontAPIError(
                    f"Front could not be reached ({type(exc).__name__}) for {endpoint}.",
                    ends_run=True,
                ) from None
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                # A login or proxy page answered with 200: it would read the same for
                # every inbox, so the run stops instead of counting failures.
                raise FrontAPIError(
                    f"Front returned HTTP 200 without JSON for {endpoint}.", 200, ends_run=True
                )
            return payload


class _CheckedClient:
    """Calls the host's ``check_active`` before every request of an injected client."""

    def __init__(self, client: Any, check_active: Callable[[], None]):
        self._client = client
        self._check_active = check_active
        self.api_url = getattr(client, "api_url", DEFAULT_API_URL)

    def get(self, path: str, params: dict | None = None) -> Any:
        self._check_active()
        return self._client.get(path, params)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\s*/?>", re.I)


def _plain(text: Any) -> str:
    """Readable text: tags dropped, entities decoded, blank runs collapsed."""
    text = str(text or "")
    if "<" in text:
        text = _TAG_RE.sub("", _BLOCK_RE.sub("\n", text))
    text = html.unescape(text).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line.strip() or (out and out[-1].strip()):
            out.append(line.strip())
    return "\n".join(out).strip()


def _when(seconds: Any) -> str:
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def _person(who: Any) -> str:
    """A teammate's name, never their email address."""
    if not isinstance(who, dict):
        return ""
    name = " ".join(p for p in (who.get("first_name"), who.get("last_name")) if p).strip()
    return name or (f"@{who['username']}" if who.get("username") else "")


def _sender(message: dict) -> str:
    """Who wrote a message: a labelled name, never an email address or phone number."""
    if message.get("is_inbound"):
        sender = next(
            (
                r
                for r in message.get("recipients") or []
                if isinstance(r, dict) and r.get("role") == "from"
            ),
            {},
        )
        name = str(sender.get("name") or "").strip()
        # Front fills name with the address when it has no name; that stays out.
        if "@" in name or name.startswith("+"):
            name = ""
        return f"Customer ({name})" if name else "Customer"
    name = _person(message.get("author"))
    return f"Teammate ({name})" if name else "Teammate"


def _message_text(message: dict) -> str:
    text = _plain(message.get("text")) or _plain(message.get("body"))
    if text:
        return text
    files = [a.get("filename") for a in message.get("attachments") or [] if isinstance(a, dict)]
    files = [str(f) for f in files if f]
    return f"(attachments: {', '.join(files)})" if files else "(no text)"


def conversation_to_row(
    conversation: dict,
    messages: list[dict],
    comments: list[dict],
    inbox_name: str,
) -> dict[str, Any] | None:
    """Render a conversation as one deterministic document row, or ``None`` when it has
    nothing readable.

    Entries are ordered by time and id, so the same conversation renders the same text
    whatever order Front sent it in. Drafts are left out.
    """
    entries: list[tuple[float, str, str, str]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("is_draft") or not m.get("id"):
            continue
        entries.append(
            (float(m.get("created_at") or 0), str(m["id"]), _sender(m), _message_text(m))
        )
    has_internal = False
    for c in comments:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        has_internal = True
        name = _person(c.get("author"))
        label = f"Internal comment ({name})" if name else "Internal comment"
        entries.append(
            (
                float(c.get("posted_at") or 0),
                str(c["id"]),
                label,
                _plain(c.get("body")) or "(no text)",
            )
        )
    if not entries:
        return None
    subject = _plain(conversation.get("subject")) or "(no subject)"
    subject_line = subject.splitlines()[0]
    title = (
        subject_line if len(subject_line) <= _TITLE_MAX else subject_line[: _TITLE_MAX - 3] + "..."
    )
    tags = sorted(
        str(t.get("name"))
        for t in conversation.get("tags") or []
        if isinstance(t, dict) and t.get("name")
    )
    header = [f"Subject: {subject_line}", f"Inbox: {inbox_name}"]
    details = [f"Status: {conversation.get('status') or 'unknown'}"]
    assignee = _person(conversation.get("assignee"))
    if assignee:
        details.append(f"Assignee: {assignee}")
    if tags:
        details.append(f"Tags: {', '.join(tags)}")
    lines = [*header, " | ".join(details), ""]
    for when, _id, who, text in sorted(entries):
        lines.append(f"--- {who}, {_when(when)}")
        lines.append(text)
        lines.append("")
    row = {
        "id": conversation["id"],
        "title": title,
        "content": "\n".join(lines).strip(),
        "url": f"https://app.frontapp.com/open/{conversation['id']}",
        # Whether the document holds internal comments as well as what the customer saw.
        "visibility": "customer_and_internal" if has_internal else "customer",
        "_deleted": False,
    }
    if inbox_name:
        row[dlt_utils.NODE_SET_COLUMN] = [inbox_name]
    return row


def deleted_row(conversation_id: str) -> dict[str, Any]:
    return {"id": conversation_id, "_deleted": True}


def _fingerprint(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]


def _tag_names(conversation: dict) -> str:
    """Tag names as one string: renaming or adding a tag does not move ``updated_at``."""
    return "|".join(
        sorted(
            str(t.get("name"))
            for t in conversation.get("tags") or []
            if isinstance(t, dict) and t.get("name")
        )
    )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
class _Run:
    """One sync run over the selected inboxes."""

    def __init__(self, client: Any, stats: dict[str, int]):
        self.client = client
        self.stats = stats
        host = urllib.parse.urlparse(getattr(client, "api_url", DEFAULT_API_URL)).netloc
        self.host = host.lower()

    def pages(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Every result of a paginated listing, following ``_pagination.next``."""
        params = dict(params or {}, limit=_PAGE_SIZE)
        seen: set[str] = set()
        endpoint = _endpoint(path)
        while True:
            payload = self.client.get(path, params)
            results = payload.get("_results")
            if not isinstance(results, list):
                raise FrontAPIError(f"Front sent no result list for {endpoint}.")
            yield from (r for r in results if isinstance(r, dict))
            nxt = (payload.get("_pagination") or {}).get("next")
            if not nxt:
                return
            parsed = urllib.parse.urlparse(str(nxt))
            # The next page must stay on the API host the token was given for.
            if parsed.scheme != "https" or parsed.netloc.lower() != self.host:
                raise FrontAPIError(f"Front sent a next page on another host for {endpoint}.")
            if nxt in seen:
                raise FrontAPIError(f"Front pages did not move forward for {endpoint}; stopping.")
            seen.add(nxt)
            path, params = parsed.path, dict(urllib.parse.parse_qsl(parsed.query))

    def inbox(self, inbox_id: str) -> dict:
        inbox = self.client.get(f"/inboxes/{inbox_id}")
        if inbox.get("id") != inbox_id:
            raise FrontAPIError("Front sent no inbox for /inboxes/{id}.")
        return inbox

    def listing(self, inbox_id: str) -> dict[str, dict]:
        conversations = {}
        for c in self.pages(f"/inboxes/{inbox_id}/conversations"):
            cid = c.get("id")
            if isinstance(cid, str) and _CONVERSATION_RE.match(cid):
                conversations[cid] = c
        return conversations

    def messages(self, conversation_id: str) -> list[dict]:
        return list(self.pages(f"/conversations/{conversation_id}/messages"))

    def comments(self, conversation_id: str) -> list[dict]:
        return list(self.pages(f"/conversations/{conversation_id}/comments"))

    def whereabouts(self, conversation_id: str) -> set[str] | None:
        """The inboxes a conversation is in now, or ``None`` when it is gone.

        A permanent delete answers 404, and a conversation merged into another answers
        301 to the survivor: both mean this id is gone.
        """
        try:
            self.client.get(f"/conversations/{conversation_id}")
        except FrontAPIError as exc:
            if exc.status == 404 or (exc.status is not None and 300 <= exc.status < 400):
                return None
            raise
        return {
            i["id"]
            for i in self.pages(f"/conversations/{conversation_id}/inboxes")
            if isinstance(i.get("id"), str)
        }


def _check_owner(state: dict, owner: str) -> None:
    """Refuse a sync state written for another Front company."""
    if state.get("owner") not in (None, owner):
        raise ValueError(
            "This sync state belongs to another Front account. Give each account its own "
            "resource_name."
        )


def sync_inboxes(
    client: Any,
    state: dict,
    inboxes: list[str],
    stats: dict[str, int],
    *,
    include_comments: bool = True,
    include_archived: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield changed conversations and tombstones for the selected inboxes.

    Every run reads the selected inboxes' conversation listings in full: Front's
    listing is not ordered by ``updated_at``, and tag changes do not move it, so only
    conversations whose ``updated_at`` or tag names changed have their messages and
    comments read again. A conversation that is trashed, permanently deleted, merged
    into another or moved out of the selected inboxes is forgotten. An inbox whose
    listing fails forgets nothing and is retried next run; it raises after three
    failed runs in a row, or when every inbox failed. A rate limit stops the run
    cleanly and the next one resumes. The state is written after the last row, and
    dlt keeps it only once the load succeeded.
    """
    for key in ("scanned", "deleted", "skipped", "failed"):
        stats.setdefault(key, 0)
    me = client.get("/me")
    owner = str(me.get("id") or "") if isinstance(me, dict) else ""
    _check_owner(state, owner)

    known: dict[str, dict] = copy.deepcopy(state.get("conversations", {}))
    inbox_states: dict[str, dict] = copy.deepcopy(state.get("inboxes", {}))
    render = {"comments": include_comments, "archived": include_archived}
    rerender = state.get("render") not in (None, render)
    selected = list(dict.fromkeys(inboxes))
    run = _Run(client, stats)

    rows: list[dict] = []
    forget: set[str] = set()
    failed: list[str] = []
    listed: dict[str, tuple[dict, str]] = {}  # conversation id -> (conversation, inbox)
    complete: set[str] = set()  # inboxes whose listing was read in full
    stopped = None

    try:
        for inbox_id in selected:
            before = inbox_states.get(inbox_id, {})
            try:
                inbox = run.inbox(inbox_id)
                found = run.listing(inbox_id)
            except FrontRateLimitError:
                raise
            except FrontAPIError as exc:
                if exc.ends_run:
                    raise
                failed.append(inbox_id)
                runs = int(before.get("failed_runs", 0)) + 1
                if runs >= _FAILED_RUNS_BEFORE_RAISE:
                    raise FrontAPIError(
                        f"A Front inbox failed {runs} runs in a row: {exc}", exc.status
                    ) from exc
                logger.warning(
                    "Front: an inbox failed (%s); the others still sync and it is retried "
                    "next run (%d of %d failed runs before this raises).",
                    exc,
                    runs,
                    _FAILED_RUNS_BEFORE_RAISE,
                )
                inbox_states[inbox_id] = {**before, "failed_runs": runs}
                continue
            name = str(inbox.get("name") or inbox_id)
            inbox_states[inbox_id] = {"name": name}
            complete.add(inbox_id)
            for cid, conversation in found.items():
                listed.setdefault(cid, (conversation, inbox_id))

        if failed and len(failed) == len(selected):
            raise FrontAPIError("Every selected Front inbox failed this run.")

        # Listed conversations: trashed or (optionally) archived ones are forgotten,
        # changed ones read again.
        for cid in sorted(listed):
            conversation, inbox_id = listed[cid]
            status = conversation.get("status")
            if status == "deleted" or (status == "archived" and not include_archived):
                if cid in known:
                    forget.add(cid)
                continue
            stamp = {
                "u": conversation.get("updated_at"),
                "t": _tag_names(conversation),
                "s": status,
                "i": inbox_id,
            }
            saved = known.get(cid)
            if saved and not rerender and all(saved.get(k) == v for k, v in stamp.items()):
                continue
            messages = run.messages(cid)
            comments = run.comments(cid) if include_comments else []
            row = conversation_to_row(
                conversation, messages, comments, inbox_states[inbox_id]["name"]
            )
            if row is None:  # nothing readable (only drafts, say)
                if cid in known:
                    forget.add(cid)
                continue
            fingerprint = _fingerprint(row)
            if saved and saved.get("f") == fingerprint:
                stats["skipped"] += 1
                known[cid] = {**stamp, "f": fingerprint}
                continue
            known[cid] = {**stamp, "f": fingerprint}
            rows.append(row)

        # Stored conversations missing from a complete listing are looked up one by one;
        # those of an inbox dropped from the selection are forgotten.
        for cid in sorted(set(known) - set(listed) - forget):
            inbox_id = known[cid].get("i")
            if inbox_id not in selected:
                forget.add(cid)
                continue
            if inbox_id not in complete:
                continue  # its inbox failed: nothing is judged missing
            now_in = run.whereabouts(cid)
            if now_in is None or not (now_in & set(selected)):
                forget.add(cid)
            # Moved to another selected inbox whose listing missed it: kept as it is.
    except FrontRateLimitError:
        stopped = "rate_limit"
        logger.warning("Front: rate limited; stopping this run, the next sync resumes.")

    stats["failed"] = len(failed) + (1 if stopped else 0)
    if failed:
        stats["failed_inboxes"] = len(failed)
    if stopped:
        stats[f"failed_{stopped}"] = 1

    for row in rows:
        stats["scanned"] += 1
        yield row
    for cid in sorted(forget):
        known.pop(cid, None)
        stats["deleted"] += 1
        yield deleted_row(cid)
    # Only reached once every row is out.
    state["owner"] = owner
    state["render"] = render
    state["conversations"] = known
    state["inboxes"] = {i: s for i, s in inbox_states.items() if i in selected}


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def front_source(
    *,
    token: str | None = None,
    inboxes: list[str] | tuple[str, ...],
    include_comments: bool = True,
    include_archived: bool = True,
    api_url: str = DEFAULT_API_URL,
    resource_name: str = "front",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Return a ``dlt`` resource that yields the selected inboxes' conversations.

    Args:
        token: A Front API token that can read the inboxes (Settings, Developers, API
            tokens). See the README for the scopes.
        inboxes: The ids of the inboxes to sync (``inb_...``). Dropping one later
            forgets its conversations.
        include_comments: Include internal comments, labelled ``Internal comment``.
            Turning it off later re-renders every conversation without them.
        include_archived: Keep archived conversations. With ``False`` they are left
            out, and forgotten once archived.
        api_url: Front's API address; the default fits most accounts. https only.
        resource_name: Table name and key of the sync state: lowercase letters and
            digits with single underscores, at most 51 characters. Give each Front
            account its own. Keep it fixed across runs.
        check_active: Optional host authorization checkpoint, called before every API
            request, before every retry wait and around every row; raising stops the sync.
        client: Object with ``get(path, params)`` returning the decoded JSON;
            ``service`` is an alias.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"`` and
        an ``_deleted`` hard-delete column. After a run, ``resource.cognee_sync_stats``
        holds counts only: ``scanned``, ``deleted``, ``skipped``, ``failed``, plus
        ``failed_inboxes`` or ``failed_rate_limit``.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Front connector requires cognee 1.6.3 or later (table-scoped document "
            "cleanup and per-row node sets). Upgrade cognee before syncing."
        )
    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    client = client if client is not None else service
    if isinstance(inboxes, str) or not inboxes:
        raise ValueError("Pass inboxes=, a non-empty list of Front inbox ids (inb_...).")
    inboxes = list(dict.fromkeys(inboxes))
    bad = [i for i in inboxes if not isinstance(i, str) or not _INBOX_RE.match(i)]
    if bad:
        raise ValueError(
            f"{len(bad)} inbox id(s) do not look like inb_...; use the id from the API "
            "(see the README), not the inbox name."
        )
    if (
        not isinstance(resource_name, str)
        or len(resource_name) > _RESOURCE_NAME_MAX
        or not _RESOURCE_NAME_RE.match(resource_name)
    ):
        raise ValueError(
            "resource_name must be lowercase letters and digits with single underscores, "
            "starting with a letter and at most 51 characters, so dlt uses it unchanged as "
            "the table name."
        )
    if client is None:
        client = FrontClient(token, api_url=api_url, check_active=check_active)
    if check_active is not None:
        client = _CheckedClient(client, check_active)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def front_conversations():
        stats.clear()
        rows = sync_inboxes(
            client,
            dlt.current.resource_state(),
            inboxes,
            stats,
            include_comments=include_comments,
            include_archived=include_archived,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = front_conversations()
    # Conversations are prose: route them through cognify (document mode).
    setattr(resource, DOCUMENT_SOURCE_ATTR, FRONT_SOURCE_NAME)
    # Keep the sync state per resource_name (and, in cognee, per dataset).
    setattr(resource, PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
