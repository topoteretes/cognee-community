"""Trakt connector for cognee: a ``dlt`` source that turns a user's viewing
history, ratings, watchlist and personal lists into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_trakt import trakt_source

    await cognee.remember(
        trakt_source(
            client_id="…",                      # from https://app.trakt.tv/settings/apps
            access_token="…",                   # from trakt_pin_flow (see the README)
        ),
        dataset_name="trakt",
        primary_key="id",
        write_disposition="merge",   # REQUIRED, see below
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Design
------
* **Auth**: OAuth. Run :func:`trakt_pin_flow` once to turn a PIN from
  ``https://trakt.tv/oauth/authorize`` into an ``access_token`` (plus
  ``refresh_token`` / ``created_at`` / ``expires_in``). Pass all four to the
  factory and the client refreshes the token itself whenever
  ``created_at + expires_in`` has passed; the refreshed material is persisted in
  dlt sync state so later runs keep refreshing. The token and the client secret
  are never logged, never in an error and never in a repr.
* **Incremental (last_activities gate)**: each run asks
  ``GET /sync/last_activities`` and compares the per-section timestamps with the
  ones stored in state. A section is re-pulled only when its timestamp moved;
  without stored activities (the first run) everything is pulled. Watched
  movies / shows are re-listed fully whenever they — or anything that appears
  inside their documents: ratings, comments, watchlist markers, list
  memberships — changed, and rows are upserted with ``merge``. Deterministic
  rows mean unchanged items keep their content hash and are not re-cognified.
* **Forget-on-delete**: the watched listings are the whole truth about history,
  so an item known from state but absent from a re-pulled listing is emitted as
  a ``_deleted`` hard-delete row (an unwatched movie or show, or episodes whose
  plays were removed) and cognee's ``orphan_cleanup`` forgets it. An unchanged
  section is never re-listed, so its items are never deleted. A key is deleted
  only when no other section still claims it: a movie that was unwatched but is
  still rated keeps its rating document.
* **Episode identity**: the watched-shows response carries season and episode
  numbers but no ids, so for every watched show the connector reads
  ``GET /shows/:id/seasons?extended=episodes`` once to map each watched episode
  to its Trakt id (the document key, shared with episode ratings) and title.
* **Several accounts**: each sync state records the Trakt username it belongs
  to, and a run by a different user is refused. Pass ``username=`` to also give
  the account its own pipeline scope and table name.
* **Node sets**: episode documents carry ``show:<slug>`` and
  ``season:<slug>-s<N>`` sets, so every episode of a show links to it (and to
  its season) in the graph without LLM extraction; show, movie and list
  documents share ``show:``/``movie:``/``list:`` sets the same way.

Trakt API limits
----------------
* Pagination is followed through the ``X-Pagination-Page-Count`` response
  header, never calculated from the requested limit (Trakt may clamp it).
* The API rate-limits with ``429`` and a ``Retry-After`` header. Waits up to a
  minute are slept through with retries; a longer one is not: the run stops
  cleanly without rows or a state change, ``cognee_sync_stats`` reports
  ``failed_rate_limit``, and the next sync resumes from the same activities.
  GET calls are also optionally budgeted per run (``max_requests``) with the
  same clean stop.

.. important::
   ``write_disposition="merge"`` is mandatory. The add pipeline defaults to
   ``"replace"``, which would wipe every earlier sync's rows on the second run.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

logger = get_logger("trakt_connector")

TRAKT_SOURCE_NAME = "trakt"

_API_BASE = "https://api.trakt.tv"
_OAUTH_BASE = "https://trakt.tv"
_OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
_MAX_RETRIES = 5
_PAGE_SIZE = 100  # requested page size; the API may clamp it
_MAX_PAGES = 2_000  # sanity cap for a runaway pagination header
_MAX_RETRY_AFTER = 60.0  # longer waits stop the run instead of sleeping
_REFRESH_MARGIN = 60.0  # refresh a little before the token actually expires
# dlt snake-cases table names, so a name it would rename could collide tables
# while keeping separate sync states. Only names dlt leaves unchanged pass.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TITLE_LENGTH = 100

RATING_TYPES = ("movies", "shows", "seasons", "episodes")
COMMENT_TYPES = ("movies", "shows")
# A listed/rated item names its media under one of these keys (an episode or
# season item also carries its show). Checked in this order of specificity.
_MEDIA_KINDS = ("movie", "episode", "season", "show")
# Sections whose listing produces documents; their keys are tracked for deletion.
_DOC_SECTIONS = (
    "watched_movies",
    "watched_shows",
    *(f"ratings_{t}" for t in RATING_TYPES),
    "lists",
)


class TraktAPIError(RuntimeError):
    """A Trakt API call failed permanently."""


class TraktAuthError(TraktAPIError):
    """Trakt rejected the access token (or a token refresh failed)."""


class TraktRateLimitError(TraktAPIError):
    """Trakt asked to wait longer than the connector sleeps (or kept answering 429)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class _TraktResponse:
    """One API reply: the parsed JSON body and lower-cased response headers."""

    __slots__ = ("headers", "json")

    def __init__(self, body: Any, headers: dict[str, str] | None):
        self.json = body
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}


# ---------------------------------------------------------------------------
# OAuth: the PIN flow, and the token exchange used by it and by refreshes
# ---------------------------------------------------------------------------
def _token_request(
    opener: Callable[[urllib.request.Request], Any], url: str, payload: dict[str, Any]
) -> dict:
    """POST an OAuth token request and return the token JSON."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # ``from None``: the HTTPError carries the request, whose body holds
        # the client secret (and, on refresh, the refresh token).
        raise TraktAuthError(
            f"Trakt rejected the OAuth token request (HTTP {exc.code}). Check the "
            "PIN, client_id and client_secret, then start the flow again."
        ) from None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise TraktAPIError(f"Trakt OAuth request failed: {type(exc).__name__}") from None


def trakt_pin_flow(
    client_id: str,
    client_secret: str,
    *,
    oauth_base: str = _OAUTH_BASE,
    prompt: Callable[[str], str] = input,
    opener: Callable[[urllib.request.Request], Any] | None = None,
    print_token: Callable[[str], None] = print,
) -> dict:
    """Run Trakt's OAuth PIN flow on the command line and return the token JSON.

    Prints the authorize URL (open it, approve the app, copy the PIN), reads the
    PIN, exchanges it for an access token and prints the token JSON. Pass the
    ``access_token`` — and, to enable automatic refresh, the ``refresh_token``,
    ``created_at`` and ``expires_in`` from the same response — to
    :func:`trakt_source`.
    """
    authorize_url = (
        f"{oauth_base}/oauth/authorize?response_type=pin"
        f"&client_id={urllib.parse.quote(client_id)}"
        f"&redirect_uri={urllib.parse.quote(_OOB_REDIRECT_URI)}"
    )
    print_token(f"1. Open this URL and approve the app:\n\n    {authorize_url}\n")
    pin = prompt("2. Enter the PIN Trakt shows you: ").strip()
    token = _token_request(
        opener or urllib.request.urlopen,
        f"{oauth_base}/oauth/token",
        {
            "code": pin,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _OOB_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    print_token("\n3. Token (it is a secret; store it somewhere safe):\n")
    print_token(json.dumps(token, indent=2))
    return token


# ---------------------------------------------------------------------------
# Minimal Trakt API client (stdlib only)
# ---------------------------------------------------------------------------
class TraktClient:
    """Small Trakt API client: ``request(path, params) -> _TraktResponse``.

    Retries 429 (honouring ``Retry-After`` up to a minute) and 5xx/network
    errors with backoff; raises :class:`TraktAuthError` for 401/403 and when a
    token refresh fails. The access token and client secret are redacted:
    never in ``__repr__``, never in an error, never logged (errors carry the
    path, not the URL).
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        *,
        refresh_token: str | None = None,
        client_secret: str | None = None,
        token_created_at: int | None = None,
        token_expires_in: int | None = None,
        oauth_base: str = _OAUTH_BASE,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ):
        self._client_id = client_id
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_secret = client_secret
        self._created_at = token_created_at
        self._expires_in = token_expires_in
        self._oauth_base = oauth_base
        self._timeout = timeout
        self._sleep = sleep
        self._now = now

    def __repr__(self) -> str:
        # The token and secret are passwords: keep them out of any repr or log.
        return "TraktClient(access_token=<redacted>)"

    def _open(self, request: urllib.request.Request):
        return urllib.request.urlopen(request, timeout=self._timeout)

    def token_info(self) -> dict[str, Any] | None:
        """The current token material, for persisting across runs (a secret)."""
        if not self._access_token:
            return None
        info: dict[str, Any] = {"access_token": self._access_token}
        if self._refresh_token:
            info["refresh_token"] = self._refresh_token
        if self._created_at is not None:
            info["created_at"] = self._created_at
        if self._expires_in is not None:
            info["expires_in"] = self._expires_in
        return info

    def adopt_token(self, info: dict[str, Any] | None) -> bool:
        """Take token material persisted by an earlier run unless it is older.

        Material saved after the last successful run is at least as fresh as
        what the caller passed, so it wins; a stale copy never overwrites a
        newer one.
        """
        if not isinstance(info, dict) or not info.get("access_token"):
            return False
        theirs, mine = info.get("created_at"), self._created_at
        if theirs is None or mine is None or theirs >= mine:
            self._access_token = info["access_token"]
            self._refresh_token = info.get("refresh_token") or self._refresh_token
            self._created_at = info.get("created_at", self._created_at)
            self._expires_in = info.get("expires_in", self._expires_in)
            return True
        return False

    def _expired(self) -> bool:
        if self._created_at is None or not self._expires_in:
            return False
        return self._now() >= self._created_at + self._expires_in - _REFRESH_MARGIN

    def _refresh(self) -> None:
        if not (self._refresh_token and self._client_id and self._client_secret):
            raise TraktAuthError(
                "The Trakt access token has expired and no refresh material was given. "
                "Run trakt_pin_flow again and pass refresh_token=, token_created_at= and "
                "token_expires_in= (and client_secret=) too."
            )
        token = _token_request(
            self._open,
            f"{self._oauth_base}/oauth/token",
            {
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": _OOB_REDIRECT_URI,
                "grant_type": "refresh_token",
            },
        )
        self._access_token = token["access_token"]
        self._refresh_token = token.get("refresh_token", self._refresh_token)
        self._created_at = token.get("created_at", self._created_at)
        self._expires_in = token.get("expires_in", self._expires_in)
        logger.info("Trakt: access token refreshed (valid for %ss).", self._expires_in)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self._client_id,
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    def request(self, path: str, params: dict[str, Any] | None = None) -> _TraktResponse:
        """GET one API endpoint; ``params`` become a sorted query string."""
        if self._expired():
            self._refresh()
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                query = "?" + urllib.parse.urlencode(sorted(clean.items()))
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(_API_BASE + path + query, headers=self._headers())
            try:
                with self._open(request) as response:
                    body = json.load(response)
                    headers = dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                # Every raise below uses ``from None``: the HTTPError carries
                # the request, and the request headers carry the token.
                headers = dict(exc.headers or {})
                status = exc.code
                retry_after = _header_float(headers.get("Retry-After"))
                if status in (401, 403):
                    raise TraktAuthError(
                        "Trakt rejected the access token. Generate a new one with "
                        "trakt_pin_flow and pass it as access_token=."
                    ) from None
                if status == 429:
                    if retry_after is not None and retry_after > _MAX_RETRY_AFTER:
                        raise TraktRateLimitError(
                            f"Trakt {path}: asked to wait {retry_after:.0f}s.", retry_after
                        ) from None
                    if attempt == _MAX_RETRIES - 1:
                        raise TraktRateLimitError(
                            f"Trakt {path}: still rate limited after {_MAX_RETRIES} tries.",
                            retry_after,
                        ) from None
                if (status == 429 or status >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = retry_after if retry_after is not None else float(2**attempt)
                    logger.warning(
                        "Trakt %s: HTTP %s, retrying in %.1fs (%d/%d).",
                        path,
                        status,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    self._sleep(delay)
                    continue
                raise TraktAPIError(f"Trakt {path} failed: HTTP {status}") from None
            except ValueError:
                # A 200 whose body is not JSON (a proxy's error page, a cut-off reply).
                raise TraktAPIError(f"Trakt {path} returned a body that is not JSON.") from None
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts, resets, RemoteDisconnected: retried like a 5xx.
                if attempt < _MAX_RETRIES - 1:
                    delay = float(2**attempt)
                    logger.warning("Trakt %s: network error, retrying in %.1fs.", path, delay)
                    self._sleep(delay)
                    continue
                reason = getattr(exc, "reason", exc)
                raise TraktAPIError(f"Trakt {path} failed: {type(reason).__name__}") from None
            return _TraktResponse(body, headers)
        raise TraktAPIError(f"Trakt {path} failed after {_MAX_RETRIES} attempts.")


class _CheckedClient:
    """Runs the host's ``check_active`` before every Trakt API call."""

    def __init__(self, client, check_active: Callable[[], None]):
        self._client = client
        self._check_active = check_active

    def __repr__(self) -> str:
        return f"_CheckedClient({self._client!r})"

    def request(self, path: str, params: dict[str, Any] | None = None) -> _TraktResponse:
        self._check_active()
        return self._client.request(path, params)


def _header_float(value: str | None) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _header_int(value: str | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def paged_items(client, path: str, **params: Any) -> list:
    """Every item of a paginated endpoint, following ``X-Pagination-Page-Count``.

    The header is the truth about the page count: Trakt may clamp the requested
    limit, so pages are never calculated from it. Endpoints that do not
    paginate return no header and are read as a single page.
    """
    items: list = []
    page = 1
    while True:
        response = client.request(path, {**params, "page": page, "limit": _PAGE_SIZE})
        data = response.json
        if not isinstance(data, list):
            raise TraktAPIError(f"Trakt {path} returned {type(data).__name__}, expected a list.")
        items.extend(data)
        page_count = _header_int(response.headers.get("x-pagination-page-count"))
        if page_count is None or page >= page_count or not data:
            return items
        page += 1
        if page > _MAX_PAGES or page > page_count:
            raise TraktAPIError(f"Trakt {path}: pagination exceeded {page_count} pages.")


# ---------------------------------------------------------------------------
# Rendering: pure functions, deterministic rows
# ---------------------------------------------------------------------------
def _ids_of(payload: dict | None) -> dict:
    return (payload or {}).get("ids") or {}


def media_key(kind: str, payload: dict | None) -> str | None:
    """A document key: ``<kind>:<trakt id>``, or ``None`` when the id is missing."""
    trakt_id = _ids_of(payload).get("trakt")
    return f"{kind}:{trakt_id}" if trakt_id is not None else None


def _label(payload: dict | None) -> str:
    """``Title (Year)`` — the year is dropped when Trakt has none."""
    title = (payload or {}).get("title") or "Untitled"
    year = (payload or {}).get("year")
    return f"{title} ({year})" if year else str(title)


def _short_title(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _TITLE_LENGTH else text[: _TITLE_LENGTH - 1].rstrip() + "…"


def _date(iso: str | None) -> str:
    """The date part of a Trakt UTC timestamp. Dates, not times: rows stay stable."""
    return (iso or "")[:10]


def _field(label: str, value: Any) -> str:
    return f"{label}: {value}" if value not in (None, "") else ""


def _join(lines: Iterable[str]) -> str:
    return "\n".join(line for line in lines if line)


def _watch_line(plays: int | None, last_watched_at: str | None) -> str | None:
    plays_part = f"{plays} play{'s' if plays != 1 else ''}" if plays is not None else ""
    when = _date(last_watched_at)
    when_part = f"last watched {when}" if when else ""
    return ", ".join(part for part in (plays_part, when_part) if part) or None


def _rating_line(rating_payload: dict | None) -> str:
    if not rating_payload or rating_payload.get("rating") is None:
        return ""
    rating = rating_payload["rating"]
    return f"My rating: {rating}/10 (rated {_date(rating_payload.get('rated_at'))})"


def _media_url(kind: str, payload: dict | None) -> str | None:
    slug = _ids_of(payload).get("slug")
    if not slug:
        return None
    return f"https://trakt.tv/movies/{slug}" if kind == "movie" else f"https://trakt.tv/shows/{slug}"


def _episode_url(show: dict, season_number: int | None, episode_number: int | None) -> str | None:
    slug = _ids_of(show).get("slug")
    if not slug or season_number is None or episode_number is None:
        return None
    return f"https://trakt.tv/shows/{slug}/seasons/{season_number}/episodes/{episode_number}"


def _list_url(list_payload: dict) -> str | None:
    trakt_id = (list_payload.get("ids") or {}).get("trakt")
    return f"https://trakt.tv/lists/{trakt_id}" if trakt_id is not None else None


def movie_to_row(entry: dict, context: dict) -> dict[str, Any] | None:
    """Render one watched-movies entry as a document row (or ``None``).

    The row is deterministic — no fetch timestamps, lists sorted — so an
    unchanged movie keeps its content hash and is not re-cognified.
    """
    movie = entry.get("movie") or {}
    key = media_key("movie", movie)
    if not key:
        return None
    rating = (context.get("ratings", {}).get("movies", {}) or {}).get(key)
    comment = (context.get("comments", {}).get("movies", {}) or {}).get(key)
    lists = sorted(context.get("list_items", {}).get(key, ()))
    watchlisted = key in context.get("watchlist", ())
    lines = [
        f"Movie: {_label(movie)}",
        _field("Runtime", f"{movie['runtime']} minutes" if movie.get("runtime") else None),
        _field("Released", _date(movie.get("released"))),
        _rating_line(rating),
        _field("Watched", _watch_line(entry.get("plays"), entry.get("last_watched_at"))),
        _field("My comment", comment),
        "On my watchlist: yes" if watchlisted else "",
        _field("On my lists", ", ".join(lists)),
        _field("Overview", movie.get("overview")),
    ]
    node_set = set()
    if _ids_of(movie).get("slug"):
        node_set.add(f"movie:{_ids_of(movie)['slug']}")
    node_set.update(f"list:{slug}" for slug in lists)
    if watchlisted:
        node_set.add("watchlist")
    return {
        "id": key,
        "title": _short_title(_label(movie)),
        "content": _join(lines),
        "url": _media_url("movie", movie),
        "cognee_node_set": sorted(node_set),
        "_deleted": False,
    }


def _episode_tag(season_number: int | None, episode_number: int | None, title: str | None) -> str:
    tag = f"S{season_number}E{episode_number}"
    return f'{tag} "{title}"' if title else tag


def show_to_row(entry: dict, episode_index: _EpisodeIndex, context: dict) -> dict[str, Any] | None:
    """Render one watched-shows entry: the show plus its watched-episode timeline.

    ``episode_index`` maps ``(season, number)`` to ``(trakt id, title)`` from
    the show's seasons listing; it is only used for episode titles here.
    """
    show = entry.get("show") or {}
    key = media_key("show", show)
    if not key:
        return None
    lists = sorted(context.get("list_items", {}).get(key, ()))
    comment = (context.get("comments", {}).get("shows", {}) or {}).get(key)
    timeline: list[tuple[str, int, int, str]] = []
    for season in entry.get("seasons") or []:
        season_number = season.get("number")
        for episode in season.get("episodes") or []:
            episode_number = episode.get("number")
            title = (episode_index.get((season_number, episode_number)) or (None, None))[1]
            watched_at = episode.get("last_watched_at") or ""
            timeline.append(
                (
                    watched_at,
                    season_number or 0,
                    episode_number or 0,
                    "- "
                    + _episode_tag(season_number, episode_number, title)
                    + f" — {_date(watched_at)}"
                    + (
                        f" ({episode['plays']} plays)"
                        if (plays := episode.get("plays")) is not None and plays != 1
                        else ""
                    ),
                )
            )
    timeline.sort(key=lambda row: (row[0], row[1], row[2]))
    lines = [
        f"Show: {_label(show)}",
        _field("Aired episodes", show.get("aired_episodes")),
        _field("Watched", _watch_line(entry.get("plays"), entry.get("last_watched_at"))),
        _field("On my lists", ", ".join(lists)),
        _field("My comment", comment),
        _field("Overview", show.get("overview")),
        "Watched episodes:" if timeline else "",
        *(row[3] for row in timeline),
    ]
    node_set = set()
    if _ids_of(show).get("slug"):
        node_set.add(f"show:{_ids_of(show)['slug']}")
    node_set.update(f"list:{slug}" for slug in lists)
    return {
        "id": key,
        "title": _short_title(_label(show)),
        "content": _join(lines),
        "url": _media_url("show", show),
        "cognee_node_set": sorted(node_set),
        "_deleted": False,
    }


def episode_to_row(
    show: dict,
    season_number: int | None,
    episode: dict,
    episode_index: _EpisodeIndex,
    context: dict,
) -> dict[str, Any] | None:
    """Render one watched episode as a document row keyed by its Trakt id.

    The watched listing carries no episode ids; ``episode_index`` (from the
    show's seasons listing) supplies them. Without a match the row falls back
    to a composite ``episode:<show id>-s<season>e<number>`` key so it is never
    lost — noted as a limitation in the README.
    """
    episode_number = episode.get("number")
    hit = episode_index.get((season_number, episode_number))
    if hit:
        key = f"episode:{hit[0]}"
        title = hit[1]
    else:
        key = f"episode:{_ids_of(show).get('trakt')}-s{season_number}e{episode_number}"
        title = None
    rating = (context.get("ratings", {}).get("episodes", {}) or {}).get(key)
    tag = _episode_tag(season_number, episode_number, title)
    lines = [
        f"Show: {_label(show)}",
        f"Episode: {tag}",
        _field("Watched", _watch_line(episode.get("plays"), episode.get("last_watched_at"))),
        _rating_line(rating),
    ]
    node_set = set()
    if (slug := _ids_of(show).get("slug")) is not None:
        node_set.add(f"show:{slug}")
        if season_number is not None:
            node_set.add(f"season:{slug}-s{season_number}")
    return {
        "id": key,
        "title": _short_title(f"{_label(show)} {tag}"),
        "content": _join(lines),
        "url": _episode_url(show, season_number, episode_number),
        "cognee_node_set": sorted(node_set),
        "_deleted": False,
    }


def rating_to_row(payload: dict, context: dict) -> dict[str, Any] | None:
    """Render a rating of an item that was never watched as its own document,
    so ratings are not lost. Watched items embed the rating in their document
    (same key) instead.
    """
    kind, media = _media_of(payload)
    if media is None:
        return None
    key = media_key(kind, media)
    if not key:
        return None
    node_set: set[str] = set()
    if kind in ("movie", "show"):
        label = f"{kind.capitalize()}: {_label(media)}"
        url = _media_url(kind, media)
        if (slug := _ids_of(media).get("slug")) is not None:
            node_set.add(f"{kind}:{slug}")
    else:
        show = payload.get("show") or {}
        slug = _ids_of(show).get("slug")
        if kind == "season":
            number = media.get("number")
            label = f"Season: {_label(show)} Season {number}"
            url = f"https://trakt.tv/shows/{slug}/seasons/{number}" if slug else None
        else:
            tag = _episode_tag(media.get("season"), media.get("number"), media.get("title"))
            label = f"Episode: {_label(show)} {tag}"
            url = _episode_url(show, media.get("season"), media.get("number"))
        if slug is not None:
            node_set.add(f"show:{slug}")
            if kind == "season" and media.get("number") is not None:
                node_set.add(f"season:{slug}-s{media['number']}")
            elif kind == "episode" and media.get("season") is not None:
                node_set.add(f"season:{slug}-s{media['season']}")
    lines = [
        label,
        _rating_line(payload),
        "Status: rated, but not watched yet",
    ]
    return {
        "id": key,
        "title": _short_title(
            _label(media) if kind in ("movie", "show") else label.split(": ", 1)[1]
        ),
        "content": _join(lines),
        "url": url,
        "cognee_node_set": sorted(node_set),
        "_deleted": False,
    }


def _listed_label(item: dict) -> str | None:
    """``Inception (2010) [movie]`` — a list/watchlist item's roster label."""
    kind, media = _media_of(item)
    if media is None:
        return None
    if kind in ("movie", "show"):
        return f"{_label(media)} [{kind}]"
    show = item.get("show") or {}
    if kind == "season":
        return f"{_label(show)} Season {media.get('number')} [season]"
    tag = _episode_tag(media.get("season"), media.get("number"), media.get("title"))
    return f"{_label(show)} {tag} [episode]"


def list_to_row(list_payload: dict, items: list) -> dict[str, Any] | None:
    """Render one personal list: its ordered roster (rank, added date)."""
    ids = list_payload.get("ids") or {}
    slug = ids.get("slug") or str(ids.get("trakt") or "")
    if not slug:
        return None
    roster: list[tuple[int, str, int, str]] = []
    for item in items:
        label = _listed_label(item)
        if label is None:
            continue
        rank = item.get("rank")
        roster.append(
            (
                rank if rank is not None else 10**9,
                item.get("listed_at") or "",
                item.get("id") or 0,
                label,
            )
        )
    roster.sort()
    lines = [
        f"List: {list_payload.get('name')}",
        _field("Description", list_payload.get("description")),
        _field("Items", list_payload.get("item_count")),
        "Roster:" if roster else "",
        *(
            f"- {rank if rank != 10**9 else '•'}. {label} — added {_date(listed_at)}"
            for rank, listed_at, _id, label in roster
        ),
    ]
    return {
        "id": f"list:{slug}",
        "title": _short_title(str(list_payload.get("name") or slug)),
        "content": _join(lines),
        "url": _list_url(list_payload),
        "cognee_node_set": [f"list:{slug}"],
        "_deleted": False,
    }


def deleted_row(key: str) -> dict[str, Any]:
    """Hard-delete marker for a stored document."""
    return {"id": key, "_deleted": True}


# ---------------------------------------------------------------------------
# Context: what the watched documents embed (ratings, comments, lists)
# ---------------------------------------------------------------------------
def _media_of(payload: dict) -> tuple[str | None, dict | None]:
    """The most specific media object a sync/list item names, and its kind."""
    for kind in _MEDIA_KINDS:
        media = payload.get(kind)
        if isinstance(media, dict):
            return kind, media
    return None, None


def _ratings_context(items: list) -> dict[str, dict]:
    """Map every rated item's key to its raw rating payload (a rating cache)."""
    out: dict[str, dict] = {}
    for payload in items or []:
        kind, media = _media_of(payload)
        if media is None:
            continue
        key = media_key(kind or "", media)
        if key:
            out[key] = payload
    return out


def _comments_context(items: list) -> dict[str, str]:
    """Map a commented movie's/show's key to the comment text."""
    out: dict[str, str] = {}
    for item in items or []:
        kind, media = _media_of(item)
        if media is None or kind not in ("movie", "show"):
            continue
        comment = (item.get("comment") or {}).get("comment") or ""
        key = media_key(kind, media)
        if key and comment.strip():
            out[key] = comment.strip()
    return out


def _copy_context(context: dict | None) -> dict:
    """A fresh copy of the cached context, with every part present."""
    context = context or {}
    ratings = context.get("ratings") or {}
    comments = context.get("comments") or {}
    return {
        "ratings": {t: dict(ratings.get(t) or {}) for t in RATING_TYPES},
        "comments": {t: dict(comments.get(t) or {}) for t in COMMENT_TYPES},
        "watchlist": list(context.get("watchlist") or ()),
        "list_items": dict(context.get("list_items") or {}),
    }


# ---------------------------------------------------------------------------
# The last_activities gate
# ---------------------------------------------------------------------------
def _dig(mapping: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(mapping, dict):
            return None
        mapping = mapping.get(key)
    return mapping


def _moved(activities: dict, previous: Any, *path: str) -> bool:
    return _dig(activities, *path) != _dig(previous, *path)


def _gate_flags(activities: dict, previous: Any) -> dict[str, bool]:
    """Which sections to re-pull: those whose last_activities timestamp moved.

    Without a previous snapshot (the first run) every section is pulled. The
    watchlist and lists/comments/ratings sections are "secondary": when any of
    them moves, the watched listings are re-pulled too, because their documents
    embed ratings, comments, watchlist markers and list memberships.
    """
    if previous is None:
        return dict.fromkeys((*_DOC_SECTIONS, "watchlist", "comments"), True)
    ratings = {f"ratings_{t}": _moved(activities, previous, t, "rated_at") for t in RATING_TYPES}
    watchlist = (
        _moved(activities, previous, "watchlist")
        or _moved(activities, previous, "watchlists")
        or any(_moved(activities, previous, t, "watchlisted_at") for t in RATING_TYPES)
    )
    lists = _moved(activities, previous, "lists")
    comments = _moved(activities, previous, "comments")
    secondary = any(ratings.values()) or watchlist or lists or comments
    return {
        "watched_movies": _moved(activities, previous, "movies", "watched_at") or secondary,
        "watched_shows": _moved(activities, previous, "episodes", "watched_at") or secondary,
        **ratings,
        "watchlist": watchlist,
        "lists": lists,
        "comments": comments,
    }


def _check_owner(state: dict, username: str) -> None:
    owner = state.get("owner")
    if owner is not None and owner != username:
        raise ValueError(
            f"This sync state belongs to another Trakt user ({owner}), not {username!r}. "
            "Give each account its own resource_name."
        )
    state["owner"] = username


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
class _SyncStopError(Exception):
    """A run that ends cleanly: ``reason`` is the stats key that says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Budget:
    """Every Trakt call of a run goes through here: counted, and stopped cleanly
    when the request budget is spent or the API's rate limit outlasts the
    client's retries. Auth errors pass straight through."""

    def __init__(self, client, max_requests: int | None, stats: dict[str, int]):
        self._client = client
        self._max_requests = max_requests
        self._stats = stats

    def request(self, path: str, params: dict[str, Any] | None = None) -> _TraktResponse:
        if self._max_requests is not None and self._stats["requests"] >= self._max_requests:
            raise _SyncStopError("failed_budget")
        self._stats["requests"] += 1
        try:
            return self._client.request(path, params)
        except TraktRateLimitError:
            raise _SyncStopError("failed_rate_limit") from None

    def get(self, path: str, **params: Any) -> Any:
        return self.request(path, params or None).json

    def all(self, path: str, **params: Any) -> list:
        return paged_items(self, path, **params)


def _record_stop(stats: dict[str, int], stop: _SyncStopError) -> None:
    stats["failed"] = 1
    stats[stop.reason] = 1
    logger.warning(
        "Trakt: sync stopped after %d request(s) (%s); nothing was confirmed and the "
        "next run resumes.",
        stats["requests"],
        stop.reason,
    )


def sync_trakt(
    client,
    state: dict,
    stats: dict[str, int] | None = None,
    max_requests: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield document rows for everything that changed since the last run.

    ``state`` (dlt resource state) holds the last ``activities`` snapshot, the
    ``known`` keys per section and the cached ``context`` (ratings, comments,
    watchlist, list memberships) of unchanged sections. It is written only after
    the last row, and dlt persists it only after the load succeeds, so a failed
    run replays. ``stats`` is counts only.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(
        items=0, deletions=0, skipped=0, failed=0, failed_auth=0, failed_rate_limit=0, requests=0
    )
    calls = _Budget(client, max_requests, stats)
    try:
        user = (calls.get("/users/me/settings") or {}).get("user") or {}
        username = user.get("username") or "me"
        _check_owner(state, username)
        activities = calls.get("/sync/last_activities") or {}
    except TraktAuthError:
        stats.update(failed=1, failed_auth=1)
        raise
    except _SyncStopError as stop:
        _record_stop(stats, stop)
        return
    flags = _gate_flags(activities, state.get("activities"))
    try:
        rows, tombstones, known, context = _collect(calls, state, flags, stats)
    except TraktAuthError:
        stats.update(failed=1, failed_auth=1)
        raise
    except _SyncStopError as stop:
        _record_stop(stats, stop)
        return
    stats["items"] += len(rows)
    stats["deletions"] += len(tombstones)
    logger.info(
        "Trakt: %s sync: %d row(s), %d deletion(s), %d request(s).",
        "incremental" if state.get("activities") is not None else "first",
        len(rows),
        len(tombstones),
        stats["requests"],
    )
    yield from rows.values()
    yield from tombstones
    # Only reached once every row is out: a run stopped mid-way saves nothing.
    state["activities"] = activities
    state["known"] = {section: sorted(keys) for section, keys in known.items() if keys}
    state["context"] = context


def _collect(
    calls: _Budget, state: dict, flags: dict[str, bool], stats: dict[str, int]
) -> tuple[dict[str, dict], list[dict], dict[str, set], dict]:
    """Pull the changed sections and render every row, plus the tombstones.

    Returns ``(rows, tombstones, known, context)``. ``known`` is the updated
    key set per document section (pulled sections replaced, untouched ones
    kept); ``context`` the updated cache used by later runs.
    """
    known: dict[str, set] = {s: set(v) for s, v in (state.get("known") or {}).items()}
    context = _copy_context(state.get("context"))
    listings: dict[str, Any] = {}
    current: dict[str, set] = {}

    if flags["watched_movies"]:
        items = calls.all("/users/me/watched/movies", extended="full")
        listings["watched_movies"] = items
        current["watched_movies"] = set()
        for entry in items:
            key = media_key("movie", entry.get("movie"))
            if key:
                current["watched_movies"].add(key)
            else:
                stats["skipped"] += 1

    if flags["watched_shows"]:
        entries = calls.all("/users/me/watched/shows", extended="full")
        pulled_shows = []
        current["watched_shows"] = set()
        for entry in entries:
            show = entry.get("show") or {}
            key = media_key("show", show)
            if not key:
                stats["skipped"] += 1
                continue
            current["watched_shows"].add(key)
            show_id = _ids_of(show).get("trakt")
            index = _seasons_index(calls, show_id)
            for season in entry.get("seasons") or []:
                for episode in season.get("episodes") or []:
                    current["watched_shows"].add(
                        _episode_key(show_id, season.get("number"), episode.get("number"), index)
                    )
            pulled_shows.append((entry, index))
        listings["watched_shows"] = pulled_shows

    for media_type in RATING_TYPES:
        if flags[f"ratings_{media_type}"]:
            items = calls.all(f"/sync/ratings/{media_type}", extended="full")
            listings[f"ratings_{media_type}"] = items
            context["ratings"][media_type] = _ratings_context(items)

    if flags["watchlist"]:
        items = calls.all("/sync/watchlist", extended="full")
        context["watchlist"] = sorted(
            key for key in (media_key(kind or "", media) for kind, media in map(_media_of, items))
            if key is not None
        )

    if flags["lists"]:
        pulled_lists = []
        current["lists"] = set()
        membership: dict[str, list[str]] = {}
        for list_payload in calls.all("/users/me/lists", extended="full"):
            ids = list_payload.get("ids") or {}
            slug = ids.get("slug") or str(ids.get("trakt") or "")
            if not slug:
                stats["skipped"] += 1
                continue
            current["lists"].add(f"list:{slug}")
            items = calls.all(f"/users/me/lists/{ids.get('trakt') or slug}/items", extended="full")
            pulled_lists.append((list_payload, items))
            for item in items:
                kind, media = _media_of(item)
                key = media_key(kind or "", media) if media is not None else None
                if key:
                    membership.setdefault(key, []).append(slug)
                else:
                    stats["skipped"] += 1
        listings["lists"] = pulled_lists
        context["list_items"] = {key: sorted(slugs) for key, slugs in membership.items()}

    if flags["comments"]:
        for media_type in COMMENT_TYPES:
            items = calls.all(f"/users/me/comments/{media_type}", extended="full")
            context["comments"][media_type] = _comments_context(items)

    # What is watched right now: the fresh listing where re-pulled, the last
    # one (stored in known) where the section did not change.
    watched_now: set[str] = set()
    for section in ("watched_movies", "watched_shows"):
        watched_now |= current.get(section) or known.get(section) or set()

    rows: dict[str, dict] = {}
    if "watched_movies" in listings:
        for entry in listings["watched_movies"]:
            row = movie_to_row(entry, context)
            if row is None:
                continue
            rows[row["id"]] = row
    if "watched_shows" in listings:
        for entry, index in listings["watched_shows"]:
            show = entry.get("show") or {}
            show_row = show_to_row(entry, index, context)
            if show_row is not None:
                rows[show_row["id"]] = show_row
            for season in entry.get("seasons") or []:
                for episode in season.get("episodes") or []:
                    episode_row = episode_to_row(
                        show, season.get("number"), episode, index, context
                    )
                    if episode_row is not None:
                        rows[episode_row["id"]] = episode_row

    # Rating documents for items that were never watched, so a rating is never
    # lost. Only needed when the watched listings were re-pulled (a ratings
    # change always re-pulls them); a rated watched item embeds its rating in
    # the watched document with the same key instead.
    if "watched_movies" in listings or "watched_shows" in listings:
        for media_type in RATING_TYPES:
            payloads = listings.get(f"ratings_{media_type}")
            if payloads is None:
                payloads = list(context["ratings"][media_type].values())
            for payload in payloads:
                kind, media = _media_of(payload)
                if media is None:
                    continue
                key = media_key(kind or "", media)
                if key is None or key in watched_now or key in rows:
                    continue
                row = rating_to_row(payload, context)
                if row is not None:
                    rows[key] = row

    if "lists" in listings:
        for list_payload, items in listings["lists"]:
            row = list_to_row(list_payload, items)
            if row is not None:
                rows[row["id"]] = row

    # Ratings sections claim only unwatched items (watched items are claimed by
    # the watched sections), whether the section was re-pulled or only cached.
    # A pulled section's claims are also its fresh ``known`` set.
    claims: dict[str, set[str]] = {}
    for section in _DOC_SECTIONS:
        if section.startswith("ratings_"):
            media_type = section.removeprefix("ratings_")
            if section in listings:
                source = (
                    media_key(kind or "", media)
                    for kind, media in map(_media_of, listings[section])
                )
            else:
                source = context["ratings"][media_type].keys()
            claims[section] = {
                key for key in source if key is not None and key not in watched_now
            }
            if section in listings:
                current[section] = set(claims[section])
        elif section in current:
            claims[section] = set(current[section])
        else:
            claims[section] = set(known.get(section) or ())

    # Forget-on-delete: an item known from a re-pulled section but absent from
    # its fresh listing is dead — unless another section still claims it (an
    # unwatched movie that is still rated keeps its rating document).
    tombstones: list[dict] = []
    for section, fresh_keys in current.items():
        gone = (known.get(section) or set()) - fresh_keys
        for other, other_claims in claims.items():
            if other != section:
                gone -= other_claims
        tombstones.extend(deleted_row(key) for key in sorted(gone))
        known[section] = set(fresh_keys)

    return rows, tombstones, known, context


_EpisodeIndex = dict[tuple[int, int], tuple[int, str | None]]


def _seasons_index(calls: _Budget, show_id: int | None) -> _EpisodeIndex:
    """Map ``(season, number)`` to ``(trakt id, title)`` for one show's episodes.

    The watched listing carries no episode ids or titles, so each watched show's
    seasons are read once. A show whose seasons cannot be read (removed from
    Trakt, say) degrades to composite episode keys rather than failing the run.
    """
    if show_id is None:
        return {}
    try:
        seasons = calls.all(f"/shows/{show_id}/seasons", extended="episodes")
    except TraktAPIError:
        logger.warning(
            "Trakt: seasons of show %s could not be read; using fallback keys.", show_id
        )
        return {}
    index: _EpisodeIndex = {}
    for season in seasons or []:
        for episode in season.get("episodes") or []:
            episode_id = _ids_of(episode).get("trakt")
            if episode_id is not None:
                position = (season.get("number"), episode.get("number"))
                index[position] = (episode_id, episode.get("title"))
    return index


def _episode_key(
    show_id: int | None, season_number: int | None, episode_number: int | None, index: dict
) -> str:
    hit = index.get((season_number, episode_number))
    if hit:
        return f"episode:{hit[0]}"
    return f"episode:{show_id}-s{season_number}e{episode_number}"


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def trakt_source(
    client_id: str | None = None,
    access_token: str | None = None,
    *,
    refresh_token: str | None = None,
    client_secret: str | None = None,
    token_created_at: int | None = None,
    token_expires_in: int | None = None,
    username: str | None = None,
    resource_name: str | None = None,
    max_requests: int | None = None,
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Create a dlt source that yields a Trakt user's history as documents.

    Args:
        client_id: Your Trakt app's client id (https://app.trakt.tv/settings/apps).
            Sent as the ``trakt-api-key`` header.
        access_token: The user's OAuth access token, from :func:`trakt_pin_flow`.
        refresh_token: The refresh token from the same response; with
            ``client_secret``, ``token_created_at`` and ``token_expires_in`` it
            lets the client refresh an expired access token itself.
        client_secret: Your Trakt app's client secret (only for the token
            refresh; never sent to the API).
        token_created_at: ``created_at`` from the token response (unix time).
        token_expires_in: ``expires_in`` from the token response (seconds).
        username: The Trakt username, for a per-account table name and pipeline
            scope. Optional; the sync state refuses a different user either way.
        resource_name: Table name and part of the sync state key. Defaults to
            ``trakt_<username>`` (or ``trakt_items``). Keep it fixed across runs.
        max_requests: Trakt requests one run may make. A run that spends them
            stops cleanly and the next run resumes. ``None`` (the default)
            lifts the limit; the API's rate limit still stops a run cleanly.
        check_active: Optional host authorization checkpoint, called before
            every Trakt API call and around every row (``guarded_rows``);
            raising from it stops the sync.
        client: Object with ``request(path, params)``; a test injection point.
            Built from the credentials when omitted. ``service`` is an alias.

    Returns:
        A dlt source with one ``<resource_name>`` resource
        (``primary_key="id"``, ``write_disposition="merge"``, ``_deleted`` hard
        delete). Pass ``write_disposition="merge"`` to ``cognee.remember`` too.
        ``source.cognee_sync_stats`` holds counts only: ``items``,
        ``deletions``, ``skipped``, ``requests``, ``failed``,
        ``failed_auth``, ``failed_rate_limit`` (and ``failed_budget``).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Trakt connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    base_client = client if client is not None else service
    if resource_name is not None and not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, starting with "
            "a letter, so dlt uses it unchanged as the table name."
        )
    if base_client is None:
        if not (client_id or "").strip() or not (access_token or "").strip():
            raise ValueError(
                "Trakt needs client_id= (your app's client id, from "
                "https://app.trakt.tv/settings/apps) and access_token= (run "
                "trakt_pin_flow to get one; see the README)."
            )
        base_client = TraktClient(
            client_id.strip(),
            access_token.strip(),
            refresh_token=refresh_token,
            client_secret=client_secret,
            token_created_at=token_created_at,
            token_expires_in=token_expires_in,
        )
    slug = re.sub(r"[^a-z0-9]+", "_", (username or "").lower()).strip("_")
    resource_name = resource_name or (f"trakt_{slug}" if slug else "trakt_items")
    worker = _CheckedClient(base_client, check_active) if check_active is not None else base_client
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the staging table on merge,
        # which is what makes cognee's orphan cleanup forget them.
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            # Typed up front so a column that is empty in a run (the text
            # columns in a deletion-only run) still exists.
            "title": {"data_type": "text", "nullable": True},
            "content": {"data_type": "text", "nullable": True},
            "url": {"data_type": "text", "nullable": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def trakt_items():
        # The caller's write_disposition overrides the one declared above. A
        # replace run of a gated delta would empty every table but the changed
        # rows, so refuse it before reading anything.
        disposition = source.resources[resource_name].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'trakt_source needs write_disposition="merge", got {disposition!r}. '
                "Its incremental runs emit only changed sections and deletions."
            )
        state = dlt.current.resource_state()
        if isinstance(base_client, TraktClient):
            base_client.adopt_token(state.get("token"))

        def _rows():
            yield from sync_trakt(worker, state, stats, max_requests)
            if isinstance(base_client, TraktClient):
                info = base_client.token_info()
                if info:
                    state["token"] = info

        yield from guarded_rows(_rows(), check_active)

    @dlt.source(name=TRAKT_SOURCE_NAME)
    def _trakt():
        return trakt_items

    source = _trakt()
    # Route rows through the document path (text -> cognify), like Notion.
    setattr(source, DOCUMENT_SOURCE_ATTR, TRAKT_SOURCE_NAME)
    # One dlt pipeline, and so one activity snapshot, per account.
    scope = f"{TRAKT_SOURCE_NAME}:{username}" if username else TRAKT_SOURCE_NAME
    setattr(source, PIPELINE_SCOPE_ATTR, scope)
    # Host-readable diagnostics contain counts only, never titles or tokens.
    source.cognee_sync_stats = stats
    source.resources[resource_name].cognee_sync_stats = stats
    return source
