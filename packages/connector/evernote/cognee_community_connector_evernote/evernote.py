"""Evernote connector for cognee — a ``dlt`` source that turns an Evernote account into memory.

Pull Evernote notes (plus their notebook and tags) into cognee, incrementally and
with forget-on-deletion — "ask my Evernote". Like the sibling Gmail/Google Drive
connectors this builds entirely on the existing dlt ingestion subsystem; the source
produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_evernote import evernote_source

    await cognee.remember(
        evernote_source(),               # EVERNOTE_AUTH_TOKEN / cached OAuth token
        dataset_name="my_evernote",
        primary_key="id",
        write_disposition="merge",       # REQUIRED — see the note below
        max_rows_per_table=0,            # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — OAuth 1.0a, the only scheme Evernote's EDAM service accepts. The
  three-legged handshake is implemented on ``requests-oauthlib`` (see
  :func:`authorize`) and the resulting opaque token is cached to disk; an existing
  developer token is accepted verbatim through ``EVERNOTE_AUTH_TOKEN``. Access is
  strictly read-only — this connector never calls create/update/delete.
* **Protocol** — Evernote's Cloud API (EDAM) is a **Thrift** service, not REST.
  ``evernote3`` supplies the generated ``evernote.edam.*`` stubs and a vendored
  pure-python ``thrift`` runtime. Its own ``evernote.api.client`` wrapper is *not*
  used and is in fact unimportable (it does ``import oauth2``, an abandoned
  package), which is why OAuth lives here instead.
* **Document-mode** — notes are prose, so the resource declares
  ``cognee_document_source = "evernote"``. ``resolve_dlt_sources`` then tags each
  row ``external_metadata["source"] = "evernote"`` and ``is_dlt_sourced`` returns
  False, so every note flows through the standard cognify entity-extraction
  pipeline instead of the deterministic dlt-row schema-context path. Rows keep the
  document contract ``{id, title, content, url}`` plus provenance columns.
* **Incremental cursor** — the account's USN (update sequence number). A run
  captures the current USN as its target, then streams sync chunks until it
  reaches it. The cursor, the set of ids already ingested, and a hash of the user's
  selection live in dlt's per-resource state, so re-running ``remember`` resumes
  where it left off and re-embeds only the delta.
* **Forget-on-delete** — a deleted note simply *stops appearing* in later sync
  chunks, so absence is not a signal. The two explicit deletion feeds are used
  instead: ``SyncChunk.expungedNotes`` (permanently deleted) and ``Note.deleted``
  (moved to Trash — Evernote's Trash is a real notebook, so a full snapshot would
  happily re-ingest it). Both emit a ``_deleted`` hard-delete marker; dlt drops
  those rows on ``merge`` and cognee's existing ``orphan_cleanup`` then purges them
  from the graph + vector + relational stores. No parallel cleanup path.
* **Scope** — Evernote's ``SyncChunkFilter`` cannot filter by notebook or tag, so
  ``notebook_guids`` / ``tag_names`` are applied client-side while scanning. The
  selection is hashed into the resource state: when it changes, the cursor resets
  and the run becomes a full re-scan, which also reconciles notes that fell out of
  scope into hard-delete markers.

.. note::
   ``write_disposition="merge"`` is **mandatory**. The add pipeline defaults to
   ``"replace"`` (drop + reload the table each run), which on the second,
   incremental sync would wipe the entire ingested notebook and leave the cursor
   pointing at a destination that no longer exists. Always pass ``"merge"``.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows from the
   dlt destination (default 50). For a real account pass ``max_rows_per_table=0``
   (unlimited) so orphan-cleanup compares against the *whole* ingested corpus
   rather than a truncated window.

.. warning::
   Evernote's EDAM API is officially deprecated ("no longer actively developed") and
   new API keys are gated behind a manual, human-reviewed request. That affects who
   can *run* this connector against a live account, not its correctness: the whole
   sync path is exercised offline in ``tests/`` against a fake Thrift client. See
   the README for what this means in practice.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("evernote_connector")

# dlt resource / staging-table name for Evernote notes.
EVERNOTE_TABLE_NAME = "evernote_notes"
EVERNOTE_SOURCE_NAME = "evernote"

# Evernote hosts. EDAM is served over HTTPS on the account's web host; sandbox has
# its own consumer key, so the host follows the credentials and is never guessed.
_PRODUCTION_HOST = "https://www.evernote.com"
_SANDBOX_HOST = "https://sandbox.evernote.com"

# Retry budget for rate-limited / transient Evernote API responses.
_MAX_RETRIES = 5
_MAX_RETRY_WAIT = 60.0

# EDAM ErrorCode values (dev.evernote.com "Error Handling"). The 1.25 generated code
# does not ship the EdamErrorCode enum, so the few codes this connector acts on are
# mirrored here; _edam_codes() prefers the SDK's own constants when a newer
# evernote3 exposes them.
# PERMISSION_DENIED, AUTHENTICATION_ERROR, LICENSE_REJECTED
_EDAM_AUTH_ERROR_CODES = frozenset({1, 4, 11})
_EDAM_NOT_FOUND_CODES = frozenset({5})  # ITEM_NOT_FOUND
_EDAM_RATE_LIMIT_CODES = frozenset({6, 9})  # RATE_LIMIT_REACHED (renumbered across EDAM versions)
_EDAM_TRANSIENT_CODES = frozenset({2, 7, 15})  # SYSTEM_ERROR, INTERNAL_MISC_ERROR, DUTY_CYCLE

# Evernote caps sync chunks at 270 entries; the docs suggest the maximum as the
# default because it minimises round trips without tripping the sync rate limit.
_DEFAULT_CHUNK_SIZE = 270

# Default throttle between getNoteContent calls, which the API rate-limits far more
# tightly than sync chunks (sync ~300 req/10s, content ~100 req/min).
_CONTENT_THROTTLE_SECONDS = 0.7

_EXTRA_HINT = (
    'The Evernote connector requires the evernote extra: pip install "cognee[evernote]" '
    "(provides dlt, evernote3 and requests-oauthlib)."
)

_TOKEN_FILE_ENV = "EVERNOTE_TOKEN_PATH"
_DEFAULT_TOKEN_FILE = os.path.join("~", ".cognee", "evernote_token.json")

# Non-breaking space (U+00A0), which Evernote's editor sprinkles through note bodies.
_NBSP = "\u00a0"


# ---------------------------------------------------------------------------
# ENML rendering
#
# Evernote note content is ENML, an XHTML dialect with a few custom elements. Raw
# tags are noise for entity extraction, so it is flattened to readable text here.
# Dependency-free on purpose (no HTML parser) and implemented as a small token
# scanner rather than a regex pipeline, because <li> bullets depend on whether the
# enclosing list was <ul> or <ol> — which needs real nesting state.
# ---------------------------------------------------------------------------
_ENML_TOKEN_RE = re.compile(
    r"<(/?)([A-Za-z][\w:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>"  # tag
    r"|([^<]+)",  # text
    re.DOTALL,
)

_ATTR_RE_TEMPLATE = r"""{name}\s*=\s*"([^"]*)"|{name}\s*=\s*'([^']*)'"""

# A line already shaped like a markdown list item (after any indent), whose indent
# encodes nesting and must survive normalisation.
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+] |\d+\. )")

# Elements dropped wholesale — they carry no readable text.
_DROP_TAGS = frozenset({"script", "style", "head", "en-crypt"})

_HEADING_PREFIX = {
    "h1": "# ",
    "h2": "## ",
    "h3": "### ",
    "h4": "#### ",
    "h5": "##### ",
    "h6": "###### ",
}

# Blocks that force a line break when they open (no close handler is needed: the
# next block opens on a new line, and the final newline is trimmed off).
_BLOCK_TAGS = frozenset({"div", "p", "section", "blockquote", "pre", "tr", "en-note"})


def _attr(attrs: str | None, name: str) -> str:
    """Read one HTML/ENML attribute value out of a raw attribute string."""
    if not attrs:
        return ""
    match = re.search(_ATTR_RE_TEMPLATE.format(name=re.escape(name)), attrs)
    if not match:
        return ""
    return html.unescape(match.group(1) if match.group(1) is not None else match.group(2))


def _line_break(out: list[str]) -> None:
    """Emit a single newline, collapsing consecutive breaks.

    Used for inline flow (``<br/>``, list items) where a blank line would break the
    construct — two markdown list items must be adjacent, not separated.

    Returns nothing; the caller reads ``break_depth`` for the nesting depth that
    was current when the break happened.
    """
    if out and out[-1] != "\n":
        out.append("\n")


def _block_break(out: list[str]) -> None:
    """Emit a blank line, so block elements separate as proper markdown paragraphs.

    Collapses repeated calls to a single blank run, so deeply nested
    ``<div>`` wrappers (Evernote's editor emits several) cost nothing.
    """
    if not out:
        return
    if out[-1] != "\n":
        out.append("\n")
    if len(out) < 2 or out[-2] != "\n":
        out.append("\n")


def _collapse(text: str) -> str:
    """Normalise rendered output: unescape leftovers, trim lines, cap blank runs.

    Leading whitespace is stripped from every line *except* list items, whose
    indent encodes nesting and is meaningful. Lines that carry only source
    indentation become empty and runs of them collapse to one blank line.
    """
    out: list[str] = []
    for raw_line in text.replace(_NBSP, " ").splitlines():
        line = raw_line.rstrip()
        if not _LIST_MARKER_RE.match(line):
            line = line.lstrip()
        if not line and out and not out[-1]:
            continue  # collapse runs of blank lines to one
        out.append(line)
    return "\n".join(out).strip()


def _render_enml(raw: str | None) -> str:
    """Flatten an ENML note body to readable plain text.

    Headings become markdown hashes, list items become ``- ``/``1. `` bullets, links
    become ``[text](href)``, ``<en-todo>`` a checkbox and ``<en-media>`` an
    ``[attachment: ...]`` marker (the binary itself is never fetched — see the
    README's out-of-scope list).
    """
    if not raw:
        return ""

    out: list[str] = []
    # Text sinks let a link buffer its own label while nested formatting writes into
    # it; the bottom of the stack is always the document itself.
    sinks: list[list[str]] = [out]
    link_hrefs: list[str] = []
    lists: list[str] = []
    counters: list[int] = []
    row_cells = 0  # cells emitted so far in the current <tr>
    # Nesting depth at the most recent line break. Evernote writes a nested list's
    # <ul> *before* its <li>, so the list stack has already grown by the time the
    # <li> fires — recording the depth here is what lets <li> recover how deeply
    # nested it actually is.
    break_depth = 0
    drop_tag = ""
    drop_depth = 0

    for match in _ENML_TOKEN_RE.finditer(raw):
        closing, name, attrs, self_closing, text = match.groups()

        # --- text node -------------------------------------------------
        if text is not None:
            if not drop_depth:
                sinks[-1].append(html.unescape(text))
            continue

        tag = name.lower()

        # --- closing tag -----------------------------------------------
        if closing:
            if drop_depth:
                if tag == drop_tag:
                    drop_depth -= 1
                    if not drop_depth:
                        drop_tag = ""
                continue
            if tag in ("ul", "ol"):
                if lists:
                    lists.pop()
                    counters.pop()
                # Leaving a nested list returns the *next* item to the enclosing
                # level; without this it would keep the inner list's indent.
                break_depth = len(lists)
            elif tag == "tr":
                row_cells = 0
            elif tag == "a" and link_hrefs and len(sinks) > 1:
                href = link_hrefs.pop()
                label = _collapse("".join(sinks.pop()))
                sinks[-1].append(f"[{label}]({href})" if label else "")
            continue

        # --- opening tag -----------------------------------------------
        if drop_tag:
            if tag == drop_tag:
                drop_depth += 1
            continue

        if tag in _DROP_TAGS:
            drop_tag, drop_depth = tag, 1
            continue

        if tag == "en-todo":
            _block_break(out)
            checked = _attr(attrs, "checked").strip().lower() == "true"
            sinks[-1].append("  " * max(len(lists) - 1, 0))
            sinks[-1].append("- [x] " if checked else "- [ ] ")
            if not self_closing:
                drop_tag, drop_depth = tag, 1
            continue

        if tag == "en-media":
            _block_break(out)
            media_type = _attr(attrs, "type") or "attachment"
            filename = _attr(attrs, "filename")
            marker = (
                f"[attachment: {filename} ({media_type})]"
                if filename
                else f"[attachment: {media_type}]"
            )
            sinks[-1].append(marker)
            continue

        if tag in _HEADING_PREFIX:
            _block_break(out)
            sinks[-1].append(_HEADING_PREFIX[tag])
            continue

        if tag in ("ul", "ol"):
            # No break here: a nested <ul> opens *inside* the preceding <li>, and a
            # blank line there would split one markdown list into two. The indent
            # of the following <li> comes from break_depth, recorded by the <li>.
            lists.append(tag)
            counters.append(0)
            break_depth = len(lists)
            continue

        if tag == "li":
            # List items stay on adjacent lines so markdown lists survive. The
            # indent comes from the depth recorded at the *previous* line break,
            # which excludes the <ul>/<ol> that wraps this very item — otherwise
            # every item would be indented one level too deep.
            _line_break(out)
            depth = max(break_depth - 1, 0)
            sinks[-1].append("  " * depth)
            if lists and lists[-1] == "ol":
                counters[-1] += 1
                sinks[-1].append(f"{counters[-1]}. ")
            else:
                sinks[-1].append("- ")
            break_depth = len(lists)
            continue

        if tag in ("td", "th"):
            if row_cells:
                sinks[-1].append(" | ")
            row_cells += 1
            continue

        if tag == "a":
            link_hrefs.append(_attr(attrs, "href"))
            sinks.append([])
            continue

        if tag == "br":
            _line_break(out)
            continue

        if tag == "hr":
            _block_break(out)
            sinks[-1].append("---")
            _block_break(out)
            continue

        if tag in _BLOCK_TAGS:
            _block_break(out)
            # A following <li> belongs to whatever list is open *after* this block,
            # so the recorded depth must reflect the stack at the break, not before.
            break_depth = len(lists)
            continue

        # Every other element — b, i, u, span, font, en-smart-tag, an unknown one —
        # contributes only its text; there is no branch to take for them.

    return _collapse("".join(out))


# ---------------------------------------------------------------------------
# OAuth 1.0a
#
# EDAM is authenticated by an opaque "authenticationToken" string rather than
# per-request HTTP signing: the token carries the OAuth key/secret pair that the
# server validates itself. So the handshake happens once here, and the resulting
# token is simply handed to the Thrift client.
# ---------------------------------------------------------------------------
def _hosts(sandbox: bool) -> tuple[str, str, str]:
    """Return ``(oauth_url, authorize_url, web_host)`` for the target environment."""
    host = _SANDBOX_HOST if sandbox else _PRODUCTION_HOST
    return f"{host}/oauth", f"{host}/OAuth.action", host


def token_file_path(path: str | None = None) -> str:
    """Where the cached OAuth token is read from and written to."""
    return os.path.expanduser(path or os.environ.get(_TOKEN_FILE_ENV) or _DEFAULT_TOKEN_FILE)


def load_cached_token(path: str | None = None) -> str | None:
    """Return the cached Evernote authentication token, if one exists."""
    try:
        with open(token_file_path(path), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return (payload.get("token") if isinstance(payload, dict) else None) or None


def save_token(token: str, path: str | None = None, **extra: Any) -> str:
    """Persist an authentication token for reuse, with owner-only permissions."""
    target = token_file_path(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump({"token": token, **extra}, handle, indent=2)
    # The token is a bearer credential; not every filesystem supports chmod.
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return target


def resolve_auth_token(token: str | None = None, path: str | None = None) -> str:
    """Resolve the token to authenticate with, in precedence order.

    The ``token=`` argument, then ``EVERNOTE_AUTH_TOKEN``, then the token cached by
    :func:`authorize`. An Evernote *developer token* is accepted in place of an OAuth
    token — both are the same opaque string to EDAM.
    """
    resolved = token or os.environ.get("EVERNOTE_AUTH_TOKEN") or load_cached_token(path)
    if not resolved:
        raise ValueError(
            "No Evernote authentication token. Run `python examples/authorize.py` "
            "(OAuth 1.0a) or set EVERNOTE_AUTH_TOKEN to an Evernote developer token."
        )
    return resolved


def authorize(
    *,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
    sandbox: bool = False,
    callback_url: str | None = None,
    token_path: str | None = None,
    verifier: str | None = None,
) -> tuple[str, str]:
    """Run the OAuth 1.0a three-legged flow and cache the access token.

    Prints Evernote's authorization URL and asks the user to paste back the
    verifier shown on the approval page. Pass ``verifier=`` to supply it directly,
    which is what a web-callback integration would do instead of prompting.

    Args:
        consumer_key: Evernote API key. Falls back to ``EVERNOTE_CONSUMER_KEY``.
        consumer_secret: Evernote API secret. Falls back to ``EVERNOTE_CONSUMER_SECRET``.
        sandbox: Use the sandbox host — its key is distinct from production's.
        callback_url: OAuth callback. Defaults to ``oob`` (paste-the-verifier flow).
        token_path: Where the token is cached. Defaults to ``~/.cognee/evernote_token.json``.
        verifier: Pre-obtained verifier, skipping the interactive prompt.

    Returns:
        ``(token, saved_path)`` — the authentication token and where it was stored.
    """
    try:
        from requests_oauthlib import OAuth1Session
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise ImportError(_EXTRA_HINT) from exc

    key = consumer_key or os.environ.get("EVERNOTE_CONSUMER_KEY")
    if not key:
        raise ValueError(
            "Evernote consumer key required: pass consumer_key= or set EVERNOTE_CONSUMER_KEY."
        )
    secret = consumer_secret or os.environ.get("EVERNOTE_CONSUMER_SECRET")

    oauth_url, authorize_url, _ = _hosts(sandbox)
    # "oob" tells the approval page to display the verifier instead of redirecting.
    session = OAuth1Session(key, client_secret=secret, callback_uri=callback_url or "oob")
    session.fetch_request_token(oauth_url)
    url, _ = session.authorization_url(authorize_url)

    if verifier is None:
        print(f"1. Open this URL and approve the application:\n\n   {url}\n")
        verifier = input("2. Paste the verification code shown on the page: ").strip()
    if not verifier:
        raise ValueError("No OAuth verifier supplied; authorization aborted.")

    session.fetch_token(oauth_url, oauth_verifier=verifier)
    # EDAM needs only the access token: it embeds the OAuth key/secret pair that
    # the server validates itself, so the token secret is not used here.
    access_token = session._client.client.access_token

    saved = save_token(access_token, token_path, sandbox=sandbox, consumer_key=key)
    logger.info("Evernote: access token cached at %s", saved)
    return access_token, saved


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------
def _edam_codes() -> tuple[frozenset[int], frozenset[int], frozenset[int], frozenset[int]]:
    """Return ``(auth, not_found, rate_limit, transient)`` EDAM code sets.

    Prefers the constants a newer evernote3 ships; falls back to the mirrored
    literals above on 1.25, whose generated code omits the enum.
    """
    try:
        from evernote.edam.limits import constants as limits

        enum = limits.EdamErrorCode
        return (
            frozenset(
                getattr(enum, name)
                for name in ("PERMISSION_DENIED", "AUTHENTICATION_ERROR", "LICENSE_REJECTED")
            ),
            frozenset({enum.ITEM_NOT_FOUND}),
            frozenset({enum.RATE_LIMIT_REACHED}),
            frozenset(
                getattr(enum, name)
                for name in (
                    "SYSTEM_ERROR",
                    "INTERNAL_MISC_ERROR",
                    "EDAM_SERVICE_DUTY_CYCLE_VIOLATION",
                )
            ),
        )
    except (ImportError, AttributeError):
        return (
            _EDAM_AUTH_ERROR_CODES,
            _EDAM_NOT_FOUND_CODES,
            _EDAM_RATE_LIMIT_CODES,
            _EDAM_TRANSIENT_CODES,
        )


def _error_family(exc: Exception) -> str:
    """Classify a Thrift error: ``auth``/``not_found``/``rate_limit``/``transient``/``fatal``.

    EDAM reports failures as Thrift application exceptions carrying an integer
    ``errorCode`` plus, for rate limits, ``rateLimitWaitSeconds``.
    """
    auth_codes, not_found_codes, rate_codes, transient_codes = _edam_codes()
    code = getattr(exc, "errorCode", None)
    if isinstance(code, int):
        if code in auth_codes:
            return "auth"
        if code in not_found_codes:
            return "not_found"
        if code in rate_codes:
            return "rate_limit"
        if code in transient_codes:
            return "transient"
        return "fatal"
    # No numeric code: a transport- or protocol-level failure is worth one retry.
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "transient"
    return "fatal"


def _retry_delay(exc: Exception, attempt: int) -> float:
    """Seconds to wait before retrying: the server's hint, else exponential backoff."""
    hint = getattr(exc, "rateLimitWaitSeconds", None)
    try:
        if hint is not None:
            return min(max(float(hint), 1.0), _MAX_RETRY_WAIT)
    except (TypeError, ValueError):
        pass
    return float(min(2**attempt, _MAX_RETRY_WAIT))


class EvernoteAuthError(RuntimeError):
    """The authentication token was rejected; re-running `authorize` is required."""


class EvernoteNotFoundError(RuntimeError):
    """An object was permanently gone (already expunged) — safe to forget."""


# ---------------------------------------------------------------------------
# Thrift store adapter
#
# Everything EDAM-specific lives behind this small surface, so the sync state
# machine below can be exercised offline against a fake and the deprecated USN
# sync API stays swappable without touching sync logic.
# ---------------------------------------------------------------------------
class EvernoteNoteStore:
    """Read-only EDAM NoteStore client over Thrift.

    Args:
        auth_token: Evernote authentication token (OAuth access token or dev token).
        sandbox: Talk to sandbox.evernote.com instead of www.evernote.com.
        note_store_path: Thrift endpoint path on the host.
        timeout: Per-request socket timeout, seconds.
        content_throttle: Minimum gap between ``getNoteContent`` calls, seconds.
    """

    def __init__(
        self,
        auth_token: str,
        *,
        sandbox: bool = False,
        note_store_path: str = "/note",
        timeout: int = 30,
        content_throttle: float = _CONTENT_THROTTLE_SECONDS,
    ) -> None:
        self.auth_token = auth_token
        self.sandbox = sandbox
        self._note_store_path = note_store_path
        self._timeout = timeout
        self._content_throttle = content_throttle
        self._last_content_call = 0.0
        self._client: Any = None
        self._chunk_filter: Any = None

    def _build(self) -> None:
        """Import the Thrift runtime lazily so this stays an optional dependency."""
        if self._client is not None:
            return
        try:
            from evernote.edam.notestore import NoteStore
            from evernote.edam.notestore import ttypes as notestore_types
            from thrift.protocol.TBinaryProtocol import TBinaryProtocol
            from thrift.transport.THttpClient import THttpClient
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise ImportError(_EXTRA_HINT) from exc

        _, _, host = _hosts(self.sandbox)
        transport = THttpClient(f"{host}{self._note_store_path}")
        # Older vendored THttpClient builds have no setTimeout.
        with contextlib.suppress(Exception):
            transport.setTimeout(self._timeout * 1000)
        self._client = NoteStore.Client(TBinaryProtocol(transport))

        # Notes only (tag names included), plus notebooks for provenance and the
        # expunged/deleted feeds. Searches, linked notebooks and loose resources are
        # deliberately excluded — they are not ingested.
        wanted = {
            "includeNotes": True,
            "includeNotebooks": True,
            "includeTags": True,
            "includeResources": False,
            "includeSearches": False,
            "includeLinkedNotebooks": False,
            "includeExpunged": True,
        }
        try:
            self._chunk_filter = notestore_types.SyncChunkFilter(**wanted)
        except TypeError:  # pragma: no cover - a newer IDL renamed the flags
            self._chunk_filter = notestore_types.SyncChunkFilter(includeNotes=True)

    def _call(self, method, *args):
        """Invoke a generated Thrift method, retrying transient failures.

        EDAM rate-limits aggressively and the generated client has no retry logic of
        its own, so a content sweep over a large note would otherwise abort the run.
        Auth failures and gone objects are translated to typed errors; everything
        else propagates once the retry budget is spent.
        """
        self._build()
        for attempt in range(_MAX_RETRIES):
            try:
                return method(*args)
            except Exception as exc:
                family = _error_family(exc)
                if family == "auth":
                    raise EvernoteAuthError(
                        "Evernote rejected the authentication token (error code "
                        f"{getattr(exc, 'errorCode', '?')}). Re-run examples/authorize.py "
                        "or refresh EVERNOTE_AUTH_TOKEN."
                    ) from exc
                if family == "not_found":
                    raise EvernoteNotFoundError(str(exc)) from exc
                if family == "fatal" or attempt == _MAX_RETRIES - 1:
                    raise
                delay = _retry_delay(exc, attempt)
                logger.warning(
                    "Evernote: %s — retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)

    # -- EDAM surface ------------------------------------------------
    def get_sync_state(self) -> int:
        """The account's current USN: the high-water mark of every change."""
        return int(self._call(self._client.getSyncState, self.auth_token))

    def get_sync_chunk(self, after_usn: int, max_entries: int):
        """One sync chunk covering every change after ``after_usn``."""
        return self._call(
            self._client.getFilteredSyncChunk,
            self.auth_token,
            int(after_usn),
            int(max_entries),
            self._chunk_filter,
        )

    def get_note_content(self, guid: str) -> str:
        """A note's ENML body.

        Sync chunks carry note *metadata* only, so content costs one extra call per
        changed note — and is the more tightly rate-limited of the two. A note
        expunged between the chunk and this fetch raises :class:`EvernoteNotFoundError`
        so the caller can tombstone it rather than fail the run.
        """
        if self._last_content_call:
            gap = time.monotonic() - self._last_content_call
            if gap < self._content_throttle:
                time.sleep(self._content_throttle - gap)
        self._last_content_call = time.monotonic()
        return self._call(self._client.getNoteContent, self.auth_token, guid)

    def list_notebooks(self) -> dict[str, str]:
        """Map of notebook GUID to name, used as provenance on ingested notes."""
        notebooks = self._call(self._client.listNotebooks, self.auth_token) or []
        return {
            notebook.guid: notebook.name
            for notebook in notebooks
            if getattr(notebook, "guid", None) and not getattr(notebook, "deleted", None)
        }


# ---------------------------------------------------------------------------
# Configuration / scope
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _EvernoteConfig:
    """The resolved, immutable ingest selection for one connector instance."""

    notebook_guids: tuple[str, ...] = ()
    tag_names: tuple[str, ...] = ()
    chunk_size: int = _DEFAULT_CHUNK_SIZE


def _scope_key(config: _EvernoteConfig) -> str:
    """Stable hash of the user's selection, stored alongside the cursor.

    Changing the selection invalidates the incremental cursor: the next run must
    re-scan from USN 0 so notes that dropped out of scope get reconciled into
    hard-delete markers instead of lingering in memory forever.
    """
    payload = json.dumps(
        {
            "notebook_guids": sorted(config.notebook_guids),
            "tag_names": sorted(config.tag_names),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------
def _iso(timestamp_ms: Any) -> str:
    """Format an EDAM millisecond timestamp as an ISO-8601 UTC string."""
    if not isinstance(timestamp_ms, (int, float)) or timestamp_ms <= 0:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def _note_url(note: Any, web_host: str) -> str:
    """Provenance URL: the clipped source when known, else the Evernote web view."""
    attributes = getattr(note, "attributes", None)
    source = getattr(attributes, "sourceURL", None) if attributes is not None else None
    return source or f"{web_host}/web/note/{getattr(note, 'guid', '')}"


def _note_to_row(note: Any, content: str, notebook_name: str, web_host: str) -> dict[str, Any]:
    """Flatten an EDAM note + its rendered body into a document row.

    ``content`` is the rendered note body *only*. Provenance (notebook, tags,
    timestamps) rides in its own columns, so a metadata-only edit — a notebook move,
    a tag rename — does not churn the content-hash ``data_id`` and trigger a
    needless re-cognify.
    """
    return {
        "id": note.guid,
        "title": note.title or "",
        "content": content,
        "url": _note_url(note, web_host),
        "notebook": notebook_name,
        "tags": ", ".join(getattr(note, "tagNames", None) or []),
        "created": _iso(getattr(note, "created", None)),
        "updated": _iso(getattr(note, "updated", None)),
        "_deleted": False,
    }


def _deleted_row(guid: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a note by guid."""
    return {"id": guid, "_deleted": True}


def _in_scope(note: Any, config: _EvernoteConfig) -> bool:
    """Apply the notebook / tag selection client-side.

    Evernote's SyncChunkFilter cannot express either, so the account is scanned
    and non-matching notes skipped here. Multiple tag names must all be present
    (AND), matching Evernote's own filter semantics.
    """
    if config.notebook_guids and getattr(note, "notebookGuid", None) not in config.notebook_guids:
        return False
    if config.tag_names:
        present = {name.lower() for name in (getattr(note, "tagNames", None) or [])}
        if not all(name.lower() in present for name in config.tag_names):
            return False
    return True


def _is_deleted(note: Any) -> bool:
    """True when a note is in Trash (``Note.deleted``) or otherwise inactive.

    Evernote's Trash is a real notebook, so trashed notes still arrive in sync
    chunks. Ingesting them would resurrect deleted content, so they are tombstoned
    like a deletion — and a restored note comes back as live on a later chunk,
    which re-ingests it.
    """
    return bool(getattr(note, "deleted", None)) or getattr(note, "active", None) is False


# ---------------------------------------------------------------------------
# Sync state machine (pure given a store + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_notes(
    store: Any,
    config: _EvernoteConfig,
    state: dict,
    *,
    web_host: str = _PRODUCTION_HOST,
) -> Iterator[dict[str, Any]]:
    """Yield notes changed since the last run, plus hard-delete markers.

    The first run — and any run after the selection changes — streams the account's
    change history from USN 0, which enumerates every note currently in scope; the
    ids it sees are reconciled against the previous run so notes that fell out of
    scope are tombstoned. Later runs start from the stored USN and emit only what
    the delta contains, taking deletions from the expunged feed and the
    ``Note.deleted`` flag.

    ``state`` is dlt's per-resource state; ``cursor_usn``, ``known_ids`` and
    ``scope_key`` are advanced in place so the next run resumes where this one
    stopped.
    """
    scope_key = _scope_key(config)
    full_scan = state.get("cursor_usn") is None or state.get("scope_key") != scope_key
    if full_scan and state:
        logger.info(
            "Evernote: ingest selection changed — re-scanning the account and "
            "reconciling the notes that fell out of scope."
        )

    after_usn = 0 if full_scan else int(state.get("cursor_usn") or 0)
    known_ids: set[str] = set(state.get("known_ids", []))
    target_usn = int(store.get_sync_state())
    notebook_names = store.list_notebooks()

    live_ids: set[str] = set()  # in-scope, non-deleted notes seen this run
    changed = 0
    tombstoned = 0

    while after_usn < target_usn:
        chunk = store.get_sync_chunk(after_usn, config.chunk_size)
        chunk_usn = int(getattr(chunk, "chunkHighUSN", 0) or 0)

        # Permanent deletions: the GUIDs Evernote expunged since the last chunk.
        for guid in getattr(chunk, "expungedNotes", None) or []:
            yield _deleted_row(guid)
            known_ids.discard(guid)
            tombstoned += 1

        # Notebooks are provenance only, but renames and new notebooks arrive on
        # the same feed, so pick them up for the notes in this chunk.
        for notebook in getattr(chunk, "notebooks", None) or []:
            guid = getattr(notebook, "guid", None)
            if guid and not getattr(notebook, "deleted", None):
                notebook_names[guid] = getattr(notebook, "name", "") or ""

        for note in getattr(chunk, "notes", None) or []:
            guid = getattr(note, "guid", None)
            if not guid or not _in_scope(note, config):
                continue

            if _is_deleted(note):
                yield _deleted_row(guid)
                known_ids.discard(guid)
                tombstoned += 1
                continue

            live_ids.add(guid)
            try:
                content = _render_enml(store.get_note_content(guid))
            except EvernoteNotFoundError:
                # Expunged between the chunk and the content fetch — forgetting is
                # the correct outcome here, not a failure.
                logger.warning("Evernote: note %s vanished mid-sync; forgetting it.", guid)
                live_ids.discard(guid)
                yield _deleted_row(guid)
                known_ids.discard(guid)
                tombstoned += 1
                continue

            if not content.strip():
                logger.warning("Evernote: note %s has no text content; skipping.", guid)
                live_ids.discard(guid)
                continue

            yield _note_to_row(
                note,
                content,
                notebook_names.get(getattr(note, "notebookGuid", ""), ""),
                web_host,
            )
            changed += 1

        # Advance the cursor. A chunk that does not move the USN forward would spin
        # this loop forever, so treat it as the end of the delta.
        if chunk_usn <= after_usn:
            logger.warning(
                "Evernote: sync chunk did not advance the USN (still %d); stopping the scan.",
                chunk_usn,
            )
            break
        after_usn = chunk_usn
        if getattr(chunk, "updateCount", None) == chunk_usn:
            break  # caught up: the server has no more recent changes

    # Reconcile only on a full scan — an incremental run never enumerates the live
    # set, so `known_ids - live_ids` there would cover every note, not just the
    # vanished ones.
    reconciled = 0
    if full_scan:
        # A full scan returning zero notes while notes were previously known almost
        # always means a transient failure rather than a genuine wipe. Treating it
        # as "all deleted" would purge the dataset and overwrite known_ids with [],
        # making the loss permanent, so skip deletion and preserve state instead.
        if known_ids and not live_ids:
            logger.warning(
                "Evernote: scan returned 0 notes but %d were known; skipping deletion "
                "this run to avoid a mass forget-on-delete on a transient failure.",
                len(known_ids),
            )
            live_ids = set(known_ids)
        else:
            for guid in sorted(known_ids - live_ids):
                yield _deleted_row(guid)
                tombstoned += 1
                reconciled += 1

        # known_ids becomes exactly what this scan saw: reconciled notes that are
        # still live stay, and anything dropped from scope above is gone. Merging
        # with the old set would leave dropped notes behind forever.
        state["known_ids"] = sorted(live_ids)
    else:
        state["known_ids"] = sorted(known_ids | live_ids)

    state["cursor_usn"] = after_usn
    state["scope_key"] = scope_key

    logger.info(
        "Evernote: synced %d note(s), %d deletion(s) (%d from reconciliation); USN %d of %d.",
        changed,
        tombstoned,
        reconciled,
        after_usn,
        target_usn,
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable (unset via false/0/no)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "")


def evernote_source(
    auth_token: str | None = None,
    *,
    notebook_guids: list[str] | None = None,
    tag_names: list[str] | None = None,
    chunk_size: int | None = None,
    sandbox: bool | None = None,
    token_path: str | None = None,
    store: Any = None,
):
    """Return a ``dlt`` resource yielding one row per Evernote note.

    Hand it to ``cognee.remember(...)`` with ``write_disposition="merge"`` and
    ``primary_key="id"``.

    Args:
        auth_token: Evernote authentication token. Falls back to
            ``EVERNOTE_AUTH_TOKEN``, then the token cached by :func:`authorize`.
        notebook_guids: Restrict ingestion to these notebook GUIDs (AND). ``None``
            ingests every notebook.
        tag_names: Restrict ingestion to notes carrying all of these tags (AND).
        chunk_size: Notes requested per sync chunk (Evernote allows up to 270).
        sandbox: Talk to the Evernote sandbox instead of production.
        token_path: Override the cached-token location.
        store: Pre-built :class:`EvernoteNoteStore`. Mainly an injection point for
            tests; when omitted one is built from the token above.

    Returns:
        A ``dlt`` resource (``evernote_notes``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column, tagged for the document ingestion path.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    config = _EvernoteConfig(
        notebook_guids=tuple(notebook_guids or ()),
        tag_names=tuple(tag_names or ()),
        chunk_size=int(chunk_size or os.environ.get("EVERNOTE_CHUNK_SIZE") or _DEFAULT_CHUNK_SIZE),
    )

    resolved_sandbox = bool(sandbox) if sandbox is not None else _env_flag("EVERNOTE_SANDBOX")
    web_host = _hosts(resolved_sandbox)[2]
    # Resolve the token eagerly so a missing one fails at construction, mid-sync,
    # where the failure would be far harder to attribute.
    resolved_token = resolve_auth_token(auth_token, token_path)

    @dlt.resource(
        name=EVERNOTE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the deletion
        # through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def evernote_notes():
        client = store or EvernoteNoteStore(resolved_token, sandbox=resolved_sandbox)
        yield from sync_notes(
            client,
            config,
            dlt.current.resource_state(),
            web_host=web_host,
        )

    resource = evernote_notes()
    # Opt into the document ingestion path: each note row (id/title/content/url)
    # becomes a text document that flows through normal cognify (LLM graph
    # extraction). resolve_dlt_sources reads this marker; it never imports this
    # connector. Sync stays incremental — hand this to remember() with
    # write_disposition="merge" (the USN delta + _deleted hard-delete).
    setattr(resource, DOCUMENT_SOURCE_ATTR, EVERNOTE_SOURCE_NAME)
    return resource
