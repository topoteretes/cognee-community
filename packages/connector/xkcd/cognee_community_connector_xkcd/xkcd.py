"""DLT source for xkcd comics (no auth, incremental watermark sync).

Fetches xkcd comics from the public JSON API and yields one text document per
comic (title, publication date, image link, alt text, and transcript), then
hands them to cognee's ingestion pipeline: the source declares
``cognee_document_source = "xkcd"``, so ``resolve_dlt_sources`` routes each row
through the standard cognify entity-extraction pipeline instead of the
deterministic dlt-row path.

Sync model
----------
* **No auth** — the xkcd JSON API is public; the comic numbered 404 does not
  exist (it responds 404) and is skipped.
* **Incremental** — the resource carries a ``dlt`` incremental cursor on the
  comic number, so the watermark lives in dlt's per-resource state. The first
  run backfills every comic; later runs fetch only comics newer than the last
  watermark. Hand the source to ``cognee.remember(...)`` with
  ``write_disposition="merge"`` (upsert by comic id) and
  ``max_rows_per_table=0`` so the whole corpus is reconciled, matching the
  other incremental document connectors (e.g. google-drive).
* **Forget-on-delete** — xkcd has no delete feed: comics are immutable once
  published and the corpus is append-only, so no hard-delete tombstones are
  emitted. A comic removed upstream mid-archive would therefore not be
  detected (merge keeps its row); the only structural anomaly the connector
  can catch is the upstream latest comic number moving behind the stored
  watermark, which it warns about without deleting anything. No-op re-syncs
  load no rows, so cognee's ``orphan_cleanup`` has no evidence to act on.
* **Be polite** — requests are paced (default one every 0.5 s) and transient
  failures (429 / 5xx / timeouts / network errors) are retried with backoff,
  honoring ``Retry-After``.
"""

import time
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("xkcd_connector")

# dlt resource / staging-table name for xkcd comics.
XKCD_TABLE_NAME = "xkcd_comics"
XKCD_SOURCE_NAME = "xkcd"

_BASE_URL = "https://xkcd.com"
# Comic #404 does not exist (xkcd's joke); the API responds 404 for it.
_MISSING_COMICS = frozenset({404})
_USER_AGENT = "cognee-xkcd-connector/0.1 (+https://github.com/topoteretes/cognee-community)"

_EXTRA_HINT = (
    'The xkcd connector requires the "dlt" extra: pip install "cognee[dlt]" '
    "(provides dlt and httpx)."
)


class _ComicNotFoundError(Exception):
    """The requested comic number does not exist upstream (HTTP 404)."""


class XkcdClient:
    """Minimal xkcd JSON API client with retry and pacing.

    The client is intentionally tiny: two endpoints (latest metadata and one
    comic by number) with polite pacing between requests and backoff on
    transient failures. Inject a pre-built client into ``xkcd_source`` to share
    one connection pool, customize ``base_url`` (proxies/mirrors), or disable
    pacing in tests.
    """

    DEFAULT_MIN_INTERVAL = 0.5
    _MAX_RETRIES = 5

    def __init__(
        self,
        base_url: str = _BASE_URL,
        timeout: float = 30.0,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        session: Any = None,
    ):
        import httpx

        self._base_url = base_url.rstrip("/")
        self._min_interval = max(0.0, float(min_interval))
        self._last_request = 0.0
        self._owns_session = session is None
        self._session = session or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )

    def close(self) -> None:
        """Close the underlying HTTP session when this client created it."""
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "XkcdClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def latest_num(self) -> int:
        """Return the comic number of the latest published comic."""
        return int(self._get_json(f"{self._base_url}/info.0.json")["num"])

    def comic(self, num: int) -> dict | None:
        """Return the comic payload, or ``None`` when the number does not exist.

        Comic #404 is known to be missing; any other 404 (e.g. a number that
        never existed) is treated the same way so a gap cannot abort a sync.
        """
        if int(num) in _MISSING_COMICS:
            return None
        try:
            return self._get_json(f"{self._base_url}/{int(num)}/info.0.json")
        except _ComicNotFoundError:
            return None

    # ------------------------------------------------------------------
    # HTTP plumbing (module-private)
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict:
        """GET ``url`` as JSON, retrying rate-limit / transient errors.

        Rate limit (429), server (5xx), timeout, and network errors are retried
        with backoff (honoring ``Retry-After``); permanent errors (404, other
        4xx) propagate. A 404 raises ``_ComicNotFound`` so the caller can treat
        a missing comic as a skip.
        """
        import httpx

        for attempt in range(self._MAX_RETRIES):
            last_attempt = attempt == self._MAX_RETRIES - 1
            self._pace()
            try:
                response = self._session.get(url)
            except httpx.TransportError:
                if last_attempt:
                    raise
                self._sleep_before_retry(url, None, attempt)
                continue

            if response.status_code == 404:
                raise _ComicNotFoundError(url)
            if response.status_code == 429 or response.status_code >= 500:
                if last_attempt:
                    response.raise_for_status()
                self._sleep_before_retry(url, response, attempt)
                continue

            response.raise_for_status()
            return response.json()

        raise AssertionError("unreachable: the retry loop always returns or raises")

    def _pace(self) -> None:
        """Sleep so consecutive requests are at least ``min_interval`` apart."""
        if self._min_interval > 0:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()

    def _sleep_before_retry(self, url: str, response: Any, attempt: int) -> None:
        delay = _retry_after(getattr(response, "headers", None), attempt)
        status = getattr(response, "status_code", "transport error")
        logger.warning(
            "xkcd: %s for %s — retrying in %.1fs (%d/%d).",
            status,
            url,
            delay,
            attempt + 1,
            self._MAX_RETRIES,
        )
        time.sleep(delay)


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def xkcd_source(
    since_num: int | None = None,
    min_interval: float = XkcdClient.DEFAULT_MIN_INTERVAL,
    client: Any = None,
):
    """Create a dlt resource that yields xkcd comics as text documents.

    Args:
        since_num: Only used when no watermark has been stored yet (first run
            for the dataset): start the backfill after this comic number. When
            omitted the whole archive is backfilled.
        min_interval: Minimum seconds between HTTP requests (politeness pacing).
        client: Pre-built ``XkcdClient`` (mainly a test-injection point); when
            omitted one is built with the arguments above.

    Returns:
        A dlt resource suitable for ``cognee.remember(...)`` — pass
        ``write_disposition="merge"`` (upsert by comic id) and
        ``max_rows_per_table=0`` (reconcile the whole corpus).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_client = client or XkcdClient(min_interval=min_interval)

    # Incremental watermark sync: the cursor value lives in dlt's per-resource
    # state, so a re-run resumes where the last one stopped. `since_num` only
    # seeds the first run for a dataset (initial_value); from then on the
    # stored watermark wins.
    cursor = dlt.sources.incremental("num", initial_value=since_num)

    @dlt.resource(
        name=XKCD_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
    )
    def xkcd_comics(cursor=cursor):
        latest = resolved_client.latest_num()
        last_value = cursor.last_value
        if last_value is not None and latest < int(last_value):
            # Comics are immutable and the archive is append-only: the latest
            # number moving backwards is a structural anomaly, not a deletion.
            # Nothing is deleted here; the corpus is kept as-is.
            logger.warning(
                "xkcd: latest comic #%d is behind the stored watermark #%s — "
                "upstream may have been rolled back; no rows are deleted.",
                latest,
                last_value,
            )
        start = int(last_value) + 1 if last_value is not None else 1
        count = 0
        for num in range(start, latest + 1):
            payload = resolved_client.comic(num)
            if payload is None:
                logger.info("xkcd: comic #%d does not exist, skipping.", num)
                continue
            count += 1
            yield _comic_to_row(payload)
        logger.info("xkcd: yielded %d comic(s) (latest #%d).", count, latest)

    resource = xkcd_comics()
    # Opt into the document ingestion path: each comic row becomes a text
    # document that flows through normal cognify (LLM graph extraction).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, XKCD_SOURCE_NAME)
    return resource


# ---------------------------------------------------------------------------
# Comic → row helpers (module-private)
# ---------------------------------------------------------------------------


def _comic_to_row(payload: dict) -> dict:
    """Flatten a comic payload into a document row.

    Only stable fields are kept (no fetch timestamps), so an unchanged comic
    keeps a stable content-hash ``data_id`` and is never re-cognified.
    """
    num = int(payload["num"])
    return {
        "id": str(num),
        "num": num,
        "url": f"{_BASE_URL}/{num}/",
        "title": _comic_title(payload, num),
        "content": _render_comic(payload),
    }


def _comic_title(payload: dict, num: int) -> str:
    """The comic title, preferring the escaped ``safe_title`` field."""
    title = (payload.get("safe_title") or payload.get("title") or "").strip()
    return title or f"xkcd #{num}"


def _render_comic(payload: dict) -> str:
    """Render the comic's metadata, alt text, and transcript as one document."""
    lines: list[str] = []
    date = _comic_date(payload)
    if date:
        lines.append(f"Published: {date}")
    if payload.get("img"):
        lines.append(f"Image: {payload['img']}")
    alt = (payload.get("alt") or "").strip()
    if alt:
        lines.append(f"Alt text: {alt}")
    transcript = (payload.get("transcript") or "").strip()
    if transcript:
        lines.append(f"Transcript:\n{transcript}")
    return "\n\n".join(lines)


def _comic_date(payload: dict) -> str:
    """Format the comic's publication date as ISO 8601, or '' when absent."""
    year, month, day = payload.get("year"), payload.get("month"), payload.get("day")
    if not (year and month and day):
        return ""
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except (TypeError, ValueError):
        return ""
