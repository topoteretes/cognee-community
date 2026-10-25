"""HackMD connector for cognee: a ``dlt`` source that turns HackMD notes into memory.

Sync the notes of a HackMD personal workspace and team workspaces into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_hackmd import hackmd_source

    await cognee.remember(
        hackmd_source(token=token, team_paths=["my-team"]),
        dataset_name="hackmd",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per note
  (``note:me:<note id>`` or ``note:team:<team path>:<note id>``): the note's
  workspace, folder path and tags, then its markdown.
* **Incremental.** Note listings are not paginated and carry each note's
  ``lastChangedAt``, so every run lists each workspace once (one request each)
  and fetches the content only of notes whose ``lastChangedAt``, title, tags,
  folder or link changed. A note whose rendered text did not change is not
  emitted again.
* **Forget-on-delete.** A note missing from its workspace listing (deleted,
  moved out, no longer shared), filtered out, or in a workspace that is no longer
  selected is emitted as a hard-delete tombstone. A 403 or 404 on a team (access
  removed) is never read as deletion: that team is left as it is.
* **A run is bounded.** HackMD counts every request against a monthly quota
  (400 on the free plan, per workspace). A used-up quota, a long rate limit, a
  spent retry wait budget or a spent request budget stops the run cleanly, keeps
  the notes it finished and reports why in ``cognee_sync_stats``.

Privacy
-------
This reads every note the token can see in the selected workspaces, private
notes included. Nothing is fetched until you construct a source and call
``remember``, and anyone with read access to the target dataset can read what was
ingested.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("hackmd_connector")

DEFAULT_BASE_URL = "https://api.hackmd.io/v1"
# HackMD usually answers in well under a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4 and 8 seconds gives up after about 15.
_RETRIES = 5
# A longer Retry-After stops the run instead of holding cognee's ingestion lock.
_MAX_RETRY_SLEEP = 60.0
# Total time one run may wait on retries before it stops cleanly.
DEFAULT_MAX_SLEEP = 300.0
# HackMD allows 100 requests every 5 minutes, and the free plan 400 a month per
# workspace, so one run stays inside one rate-limit window.
DEFAULT_MAX_REQUESTS = 100
# The note was deleted. A 403 only means access was removed.
_DELETED = 404
_FORBIDDEN = 403
_RESOURCE_NAME = re.compile(r"[a-z0-9_]{1,64}")
_STATS = (
    "scanned",
    "emitted",
    "unchanged",
    "deleted",
    "gone",
    "unreadable",
    "skipped",
    "failed",
    "failed_auth",
    "failed_budget",
    "failed_quota",
    "failed_rate_limit",
)


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the token or response bodies.
# ---------------------------------------------------------------------------
class HackMDSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class HackMDAuthError(HackMDSourceError):
    """HackMD rejected the API token (HTTP 401)."""


class HackMDRateLimitedError(HackMDSourceError):
    """HackMD kept answering HTTP 429, asked for a long wait, or the wait budget is spent."""


class HackMDQuotaError(HackMDSourceError):
    """The workspace's monthly API quota is used up (HTTP 429 ``quota_exceeded``)."""


class HackMDAPIError(HackMDSourceError):
    """HackMD answered with an error or an unexpected body, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _clean_token(token: str | None) -> str:
    token = (token or "").strip()
    # Reject early and without echoing the value: httpx would put an invalid
    # header value, token included, into its exception.
    if not token or not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("The HackMD API token is empty or has an invalid format")
    return token


def _retry_after(response: Any) -> float | None:
    try:
        return max(float(response.headers.get("Retry-After")), 0.0)
    except (TypeError, ValueError):
        return None


def _quota_exceeded(response: Any) -> bool:
    try:
        error = response.json().get("error")
    except ValueError:
        return False
    return isinstance(error, dict) and error.get("code") == "quota_exceeded"


class HackMDClient:
    """Minimal synchronous client for the HackMD API.

    ``get`` returns the decoded JSON body. Tests and hosts can pass any object with
    the same method to :func:`hackmd_source` instead.
    """

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_sleep: float = DEFAULT_MAX_SLEEP,
        check_active: Callable[[], None] | None = None,
    ):
        import httpx

        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {_clean_token(token)}"}
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._sleep = sleep
        self._max_sleep = max_sleep
        self._check_active = check_active
        self._slept = 0.0

    def __repr__(self) -> str:
        return "HackMDClient(token=<redacted>)"

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _wait(self, delay: float) -> None:
        if self._slept + delay > self._max_sleep:
            raise HackMDRateLimitedError("HackMD retries used up the wait budget of this run")
        self._slept += delay
        self._sleep(delay)
        if self._check_active is not None:
            self._check_active()

    def get(self, path: str) -> Any:
        import httpx

        attempt = 0
        while True:
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(self.base_url + path, headers=self._headers)
            except httpx.TransportError as exc:
                if last:
                    raise HackMDAPIError(f"HackMD request failed: {type(exc).__name__}") from exc
                self._wait(float(2**attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429:
                if _quota_exceeded(response):
                    raise HackMDQuotaError("HackMD API quota exceeded (HTTP 429)")
                wait = _retry_after(response)
                if last or (wait is not None and wait > _MAX_RETRY_SLEEP):
                    raise HackMDRateLimitedError("HackMD rate limit exceeded (HTTP 429)")
                self._wait(wait if wait is not None else float(2**attempt))
            elif status >= 500 and not last:
                wait = _retry_after(response)
                self._wait(min(wait if wait is not None else float(2**attempt), _MAX_RETRY_SLEEP))
            elif status == 401:
                raise HackMDAuthError("HackMD rejected the API token (HTTP 401)")
            elif status != 200:
                raise HackMDAPIError(f"HackMD request failed: HTTP {status}", status=status)
            else:
                try:
                    return response.json()
                except ValueError:
                    raise HackMDAPIError("HackMD answered with a body that is not JSON") from None
            attempt += 1


# ---------------------------------------------------------------------------
# Rendering: deterministic, no volatile fields.
# ---------------------------------------------------------------------------
def _folder_path(note: dict) -> list[str]:
    """Folder names from the top folder down, following each folder's ``parentId``."""
    folders = [f for f in note.get("folderPaths") or [] if isinstance(f, dict) and f.get("id")]
    by_parent = {f.get("parentId"): f for f in folders}
    names: list[str] = []
    seen: set[str] = set()
    folder = by_parent.get(None)
    while folder is not None and folder["id"] not in seen:
        seen.add(folder["id"])
        names.append(str(folder.get("name") or "").strip())
        folder = by_parent.get(folder["id"])
    return [name for name in names if name]


def _tags(note: dict) -> list[str]:
    return [str(tag).strip() for tag in note.get("tags") or [] if str(tag).strip()]


def _workspace_label(workspace: str) -> str:
    return "Personal" if workspace == "me" else f"Team {workspace.split(':', 1)[1]}"


def _row_id(workspace: str, note_id: str) -> str:
    return f"note:{workspace}:{note_id}"


def render_note(workspace: str, note: dict, content: str) -> dict[str, Any]:
    title = str(note.get("title") or "").strip() or "Untitled note"
    header = [f"Workspace: {_workspace_label(workspace)}"]
    if folders := _folder_path(note):
        header.append("Folder: " + " / ".join(folders))
    if tags := _tags(note):
        header.append("Tags: " + ", ".join(tags))
    return {
        "id": _row_id(workspace, note["id"]),
        "title": title,
        "content": "\n".join(header) + "\n\n" + content.strip(),
        "url": str(note.get("publishLink") or ""),
        "_deleted": False,
    }


def _digest(value: Any) -> str:
    return hashlib.md5(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _listing_meta(note: dict) -> str:
    """What the rendered text takes from the listing, besides the content."""
    return _digest([note.get("title"), _tags(note), _folder_path(note), note.get("publishLink")])


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _HackMDConfig:
    personal: bool
    team_paths: tuple[str, ...]
    tags: tuple[str, ...] = ()
    folders: tuple[str, ...] = ()
    max_requests: int = DEFAULT_MAX_REQUESTS

    def workspaces(self) -> list[str]:
        return (["me"] if self.personal else []) + [f"team:{p}" for p in self.team_paths]


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the notes finished so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _notes_path(workspace: str) -> str:
    if workspace == "me":
        return "/notes"
    return f"/teams/{quote(workspace.split(':', 1)[1], safe='')}/notes"


def _wanted(note: dict, config: _HackMDConfig) -> bool:
    if config.tags and not set(config.tags) & set(_tags(note)):
        return False
    return not config.folders or bool(set(config.folders) & set(_folder_path(note)))


class _Walker:
    """One run over the selected workspaces, writing progress into ``state``."""

    def __init__(
        self,
        client: Any,
        config: _HackMDConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.known: dict[str, dict] = state.setdefault("workspaces", {})
        self.requests = 0
        # Set once a note is finished; the budget never stops a run before that.
        self.progressed = False

    def _call(self, path: str) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.get(path)
        except HackMDQuotaError:
            raise _CutShortError("quota") from None
        except HackMDRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except HackMDAuthError:
            # Revoked during the run: keep what is done. On the first request it is
            # a wrong token, which raises.
            if self.requests > 1:
                raise _CutShortError("auth") from None
            raise

    def _forget(self, workspace: str, note_ids: list[str]) -> Iterator[dict]:
        notes = self.known.get(workspace, {}).get("notes", {})
        for note_id in note_ids:
            notes.pop(note_id, None)
            self.stats["deleted"] += 1
            yield {"id": _row_id(workspace, note_id), "_deleted": True}

    # -- the listings ------------------------------------------------------
    def _list(self, workspace: str) -> list[dict] | None:
        """The workspace's wanted notes, or ``None`` when its team cannot be read."""
        try:
            listing = self._call(_notes_path(workspace))
        except HackMDAPIError as exc:
            if workspace == "me" or exc.status not in (_FORBIDDEN, _DELETED):
                raise
            if workspace not in self.known:
                # Never synced: most likely a typo or a team the token was never given.
                raise HackMDSourceError(
                    f"HackMD team {workspace.split(':', 1)[1]} cannot be read (HTTP {exc.status})"
                ) from exc
            logger.warning("HackMD: %s answered %s, left unchanged.", workspace, exc.status)
            self.stats["unreadable"] += 1
            return None
        # A 200 without the list must not read as "nothing exists".
        if not isinstance(listing, list):
            raise HackMDAPIError("HackMD answered a note listing that is not a list")
        notes = []
        for note in listing:
            if not isinstance(note, dict) or not isinstance(note.get("id"), str):
                self.stats["skipped"] += 1
                continue
            self.stats["scanned"] += 1
            if _wanted(note, self.config):
                notes.append(note)
        if not listing and self.known.get(workspace, {}).get("notes"):
            return self._confirm_empty(workspace)
        self.state.get("empty", {}).pop(workspace, None)
        return notes

    def _confirm_empty(self, workspace: str) -> list[dict] | None:
        """An empty listing of a workspace that had notes is confirmed by the next run."""
        empty = self.state.setdefault("empty", {})
        if empty.pop(workspace, False):
            return []
        empty[workspace] = True
        logger.warning(
            "HackMD listed no notes in %s although some were synced before; nothing is "
            "forgotten until the next run confirms it.",
            workspace,
        )
        return None

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        selected = self.config.workspaces()
        # A workspace that is no longer selected is forgotten as a whole.
        for workspace in [w for w in self.known if w not in selected]:
            yield from self._forget(workspace, list(self.known[workspace].get("notes", {})))
            del self.known[workspace]
        changed: list[tuple[str, dict]] = []
        for workspace in selected:
            notes = self._list(workspace)
            if notes is None:
                continue
            known = self.known.setdefault(workspace, {}).setdefault("notes", {})
            listed = {note["id"]: note for note in notes}
            # Decided from the listing, so a run cut short later still forgets them.
            yield from self._forget(workspace, [n for n in known if n not in listed])
            for note_id, note in listed.items():
                entry = known.get(note_id)
                if (
                    entry
                    and entry.get("changed") == note.get("lastChangedAt")
                    and entry.get("meta") == _listing_meta(note)
                ):
                    self.stats["unchanged"] += 1
                else:
                    changed.append((workspace, note))
        yield from self._fetch(changed)

    def _fetch(self, changed: list[tuple[str, dict]]) -> Iterator[dict]:
        # Start after the note a cut-short run reached, so every one gets its turn.
        order = sorted(changed, key=lambda item: (item[0], item[1]["id"]))
        resume = self.state.get("resume")
        if resume:
            order = [i for i in order if [i[0], i[1]["id"]] > resume] + [
                i for i in order if [i[0], i[1]["id"]] <= resume
            ]
        for workspace, note in order:
            yield from self._fetch_note(workspace, note)
            self.state["resume"] = [workspace, note["id"]]
            self.progressed = True
        self.state.pop("resume", None)

    def _fetch_note(self, workspace: str, note: dict) -> Iterator[dict]:
        note_id = note["id"]
        path = f"{_notes_path(workspace)}/{quote(note_id, safe='')}"
        try:
            body = self._call(path)
        except HackMDAPIError as exc:
            if exc.status == _DELETED:
                # Deleted between the listing and this read.
                self.stats["gone"] += 1
                if note_id in self.known[workspace]["notes"]:
                    yield from self._forget(workspace, [note_id])
                return
            if exc.status != _FORBIDDEN:
                raise
            logger.warning("HackMD: note %s answered 403, left unchanged.", note_id)
            self.stats["unreadable"] += 1
            return
        if not isinstance(body, dict) or not isinstance(body.get("content"), str):
            raise HackMDAPIError("HackMD answered a note without its content")
        row = render_note(workspace, note, body["content"])
        known = self.known[workspace]["notes"]
        previous = known.get(note_id)
        known[note_id] = {
            "changed": note.get("lastChangedAt"),
            "meta": _listing_meta(note),
            "hash": _digest(row),
        }
        if previous and previous.get("hash") == known[note_id]["hash"]:
            self.stats["unchanged"] += 1
            return
        self.stats["emitted"] += 1
        yield row


def _iter_rows(
    client: Any,
    config: _HackMDConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict.

    A rejected token raises :class:`HackMDAuthError`, which is never read as "gone".
    """
    for key in _STATS:
        stats.setdefault(key, 0)
    try:
        yield from _Walker(client, config, state, stats, check_active).rows()
    except _CutShortError as cut:
        # A spent budget is a planned pause, not a failure; failed_budget says why.
        if cut.reason != "budget":
            stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("HackMD sync stopped early (%s); it resumes on the next run.", cut.reason)
        return
    logger.info(
        "HackMD: %d note(s) emitted, %d unchanged, %d deleted.",
        stats["emitted"],
        stats["unchanged"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if slug != value:
        # Team paths are case sensitive, so the raw value keeps them apart.
        slug += "_" + hashlib.sha256(value.encode()).hexdigest()[:8]
    return slug


def _default_resource_name(config: _HackMDConfig) -> str:
    parts = (["me"] if config.personal else []) + [_slug(p) for p in config.team_paths]
    name = "hackmd_" + "_".join(parts)
    if config.tags or config.folders:
        # Two filtered syncs into one dataset must not share state and cleanup.
        filters = _digest([sorted(config.tags), sorted(config.folders)])[:8]
        name += "_" + filters
    if len(name) > 64:
        name = name[:55] + "_" + _digest(name)[:8]
    return name


def _names(values: list[str] | None, what: str) -> tuple[str, ...]:
    names = tuple(str(value).strip() for value in values or ())
    if any(not name for name in names):
        raise ValueError(f"HackMD {what} must not be empty")
    return names


def hackmd_source(
    *,
    token: str | None = None,
    personal: bool = True,
    team_paths: list[str] | None = None,
    tags: list[str] | None = None,
    folders: list[str] | None = None,
    base_url: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per HackMD note.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        token: HackMD API token (Settings > API).
        personal: Sync the notes of the token's personal workspace.
        team_paths: Team workspaces to sync, by the path in their URL
            (``hackmd.io/team/<path>``). A team that cannot be read on its first
            sync raises.
        tags: Only notes carrying at least one of these tags.
        folders: Only notes inside a folder (at any depth) with one of these names.
        base_url: API base URL, default ``https://api.hackmd.io/v1``.
        max_requests: Request budget of one run, checked once a note is finished.
            A bigger workspace finishes over several runs; a spent budget sets
            ``failed_budget`` but not ``failed``.
        resource_name: Staging table name, which scopes this sync's state and
            cleanup: lowercase letters, digits and underscores, up to 64. Defaults
            to one derived from the workspaces and filters.
        check_active: Optional host authorization checkpoint, called before each
            request, during retry waits and around each row.
        service: Pre-built client (see :class:`HackMDClient`). Mainly an injection
            point for tests and hosts. ``client`` is accepted as an alias.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    paths = _names(team_paths, "team paths")
    for path in paths:
        if "/" in path or any(c.isspace() for c in path):
            raise ValueError(f"HackMD team path {path!r} must be the path only, not a URL")
    if not personal and not paths:
        raise ValueError("Select something to sync: personal notes, team_paths, or both.")
    config = _HackMDConfig(
        personal=personal,
        team_paths=tuple(dict.fromkeys(paths)),
        tags=_names(tags, "tags"),
        folders=_names(folders, "folders"),
        max_requests=int(max_requests),
    )
    if resource_name is not None and not _RESOURCE_NAME.fullmatch(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, up to 64 characters"
        )
    name = resource_name or _default_resource_name(config)
    service = service or client
    if service is None:
        # Checked now, so a bad token fails here rather than inside the pipeline.
        token = _clean_token(token) if token else None
        if not token:
            raise ValueError("A HackMD API token is required: pass token=, or a pre-built service.")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def hackmd_documents():
        stats.clear()
        state = dlt.current.resource_state()
        run_client = service or HackMDClient(
            token, base_url or DEFAULT_BASE_URL, check_active=check_active
        )
        try:
            rows = _iter_rows(run_client, config, state, stats, check_active)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if service is None:
                run_client.close()

    resource = hackmd_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "hackmd")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
