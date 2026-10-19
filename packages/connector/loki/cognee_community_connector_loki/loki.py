"""Grafana Loki connector for cognee: a dlt resource that turns your logs into memory.

It reads log lines with LogQL selectors through Loki's ``query_range`` API and groups
them into one document per stream and time bucket, so the lines around an error stay
together. It is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        loki_source(
            base_url="http://localhost:3100",
            selectors=['{service_name="checkout", level=~"error|warn"}'],
            resource_name="loki_checkout",
        ),
        dataset_name="logs",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )
"""

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("loki_connector")

_NS_PER_SECOND = 1_000_000_000
_NS_PER_MINUTE = 60 * _NS_PER_SECOND
_NS_PER_HOUR = 60 * _NS_PER_MINUTE
_NS_PER_DAY = 24 * _NS_PER_HOUR

DEFAULT_BUCKET_MINUTES = 5
DEFAULT_LOOKBACK_HOURS = 1
DEFAULT_KEEP_FOR_DAYS = 7
# loki's default max_query_length is 721h and queries are split by 30m-1h on the server,
# so one hour per request stays well inside every default limit
DEFAULT_QUERY_WINDOW_MINUTES = 60
# loki's default max_entries_limit_per_query
DEFAULT_PAGE_LIMIT = 5000
DEFAULT_MAX_LINES = 100_000
DEFAULT_MAX_LINES_PER_BUCKET = 1000
# buckets read again on every run, to pick up lines that arrived late
DEFAULT_OVERLAP_BUCKETS = 1
# labels tried in order for a readable document title
_TITLE_LABELS = ("service_name", "service", "app", "job", "container", "namespace")
# (connect, read) seconds
_TIMEOUT = (10, 60)
# attempts per request on 429, 5xx or network errors, backing off 1s then 2s
_RETRIES = 3


class LokiSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class LokiAPIError(LokiSourceError):
    """Loki answered with an error, or could not be reached."""


class LokiAuthError(LokiSourceError):
    """Loki rejected the credentials (HTTP 401 or 403)."""


class LokiQueryError(LokiSourceError):
    """Loki rejected the query (HTTP 400), for example a malformed selector."""


class LokiClient:
    """Minimal synchronous client for Loki's ``query_range`` endpoint.

    Tests can pass any object with the same ``query_range`` method instead.
    """

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        tenant_id: str | None = None,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        url = (base_url or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("The Loki base_url must start with http:// or https://")
        if token and (username or password):
            raise ValueError("Pass either a bearer token or a username and password, not both")
        if bool(username) != bool(password):
            raise ValueError("Basic auth needs both a username and a password")
        self._url = f"{url}/loki/api/v1/query_range"
        self._auth = (username, password) if username else None
        self._headers = {
            "Accept": "application/json",
            # without it loki 3 folds structured metadata and parsed labels into the stream
            # labels, so every distinct trace id or parsed value would become its own stream
            "X-Loki-Response-Encoding-Flags": "categorize-labels",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        if tenant_id:
            self._headers["X-Scope-OrgID"] = tenant_id
        self._session = session or requests.Session()
        self._sleep = sleep

    def __repr__(self) -> str:
        return "LokiClient(<redacted>)"

    def query_range(self, query: str, start: int, end: int, limit: int) -> list[dict]:
        """Return the streams with lines in ``[start, end)``, oldest first, at most ``limit``."""
        params = {
            "query": query,
            "start": str(start),
            "end": str(end),
            "limit": str(limit),
            "direction": "forward",
        }
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = self._session.get(
                    self._url,
                    params=params,
                    headers=self._headers,
                    auth=self._auth,
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as exc:
                if last:
                    raise LokiAPIError(f"Loki request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue
            if (response.status_code == 429 or response.status_code >= 500) and not last:
                self._sleep(2**attempt)
                continue
            return self._parse(response)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _parse(response: Any) -> list[dict]:
        status = response.status_code
        if status in (401, 403):
            raise LokiAuthError(f"Loki rejected the credentials (HTTP {status})")
        if status == 400:
            # loki explains a rejected query in a short plain-text body
            reason = (response.text or "").strip()[:300]
            raise LokiQueryError(f"Loki rejected the query: {reason}")
        if status != 200:
            raise LokiAPIError(f"Loki request failed: HTTP {status}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or payload.get("status") != "success":
            raise LokiAPIError("Loki sent an unreadable answer")
        if data.get("resultType") != "streams":
            raise LokiQueryError(
                "The selector returned metrics, not log lines; use a log query such as "
                '{service_name="checkout"}'
            )
        return data.get("result") or []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def _iso(ns: int) -> str:
    moment = datetime.fromtimestamp(ns // _NS_PER_SECOND, tz=timezone.utc)
    millis = (ns % _NS_PER_SECOND) // 1_000_000
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{millis:03d}Z"


def _minute(ns: int) -> str:
    return f"{datetime.fromtimestamp(ns // _NS_PER_SECOND, tz=timezone.utc):%Y-%m-%d %H:%M}"


def _label_text(labels: dict[str, str]) -> str:
    return ", ".join(f'{key}="{value}"' for key, value in sorted(labels.items()))


def _title_name(labels: dict[str, str]) -> str:
    for key in _TITLE_LABELS:
        if labels.get(key):
            return labels[key]
    return _label_text(labels) or "unlabelled stream"


def render_bucket(
    selector: str,
    labels: dict[str, str],
    start: int,
    end: int,
    lines: list[tuple[int, str, dict]],
    *,
    max_lines: int = DEFAULT_MAX_LINES_PER_BUCKET,
) -> dict[str, Any]:
    """One document for the lines of one stream in ``[start, end)``, oldest first."""
    shown = lines[:max_lines]
    body = []
    for ts, line, metadata in shown:
        # loki 3 adds detected_level to every line; it says nothing a level label already does
        if "level" in labels and metadata.get("detected_level") == labels["level"]:
            metadata = {k: v for k, v in metadata.items() if k != "detected_level"}
        extra = f" {_label_text(metadata)}" if metadata else ""
        body.append(f"{_iso(ts)} {line}{extra}")
    if len(lines) > len(shown):
        body.append(f"... {len(lines) - len(shown)} more lines in this window are not shown")
    header = [
        f"Selector: {selector}",
        f"Labels: {_label_text(labels)}",
        f"Window: {_iso(start)} to {_iso(end)}",
        f"Lines: {len(lines)}",
    ]
    return {
        "id": f"loki:{_hash([selector, labels])}:{start}",
        "title": f"{_title_name(labels)} logs {_minute(start)} UTC",
        "content": "\n".join([*header, "", *body]),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _line_metadata(value: list) -> dict[str, str]:
    """The structured metadata and parsed labels Loki sent with one line, if any."""
    extra = value[2] if len(value) > 2 and isinstance(value[2], dict) else {}
    if "structuredMetadata" in extra or "parsed" in extra:
        return {**(extra.get("structuredMetadata") or {}), **(extra.get("parsed") or {})}
    return extra


def _entries(streams: list[dict]) -> Iterator[tuple[int, str, dict, str, dict]]:
    """Flatten a ``query_range`` answer into (ts, stream key, labels, line, metadata)."""
    for stream in streams:
        labels = stream.get("stream") or {}
        key = _hash(labels)
        for value in stream.get("values") or []:
            yield int(value[0]), key, labels, value[1], _line_metadata(value)


def fetch_window(
    client: Any, selector: str, start: int, end: int, *, page_limit: int
) -> list[tuple[int, str, dict, str, dict]]:
    """Every line of ``selector`` in ``[start, end)``.

    A full answer can't be paged by its newest timestamp: when Loki cuts an answer at
    ``page_limit`` it does not keep the oldest lines across all streams, so one stream
    can stop earlier than another. An answer below the limit is complete, so a full one
    is thrown away and the range is read again in two halves.
    """
    pending = [(start, end)]
    collected = []
    while pending:
        low, high = pending.pop()
        page = list(_entries(client.query_range(selector, low, high, page_limit)))
        if len(page) < page_limit or high - low <= 1:
            if len(page) >= page_limit:
                logger.warning(
                    "Loki has more than %d lines in one nanosecond; some are left out. "
                    "Raise page_limit to read them all.",
                    page_limit,
                )
            collected.extend(entry for entry in page if low <= entry[0] < high)
            continue
        middle = low + (high - low) // 2
        # newer half first on the stack, so ranges are read oldest first
        pending.extend([(middle, high), (low, middle)])
    return collected


def _windows(start: int, end: int, size: int) -> Iterator[tuple[int, int]]:
    while start < end:
        yield start, min(start + size, end)
        start += size


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    selectors: list[str],
    now: int,
    bucket: int,
    lookback: int,
    keep_for: int | None,
    query_window: int,
    overlap_buckets: int,
    page_limit: int,
    max_lines: int,
    max_lines_per_bucket: int,
) -> Iterator[dict]:
    """Yield the changed and expired documents. Pure of dlt, so tests drive it with a dict."""
    for key in ("lines", "emitted", "skipped", "deleted", "cut_short"):
        stats.setdefault(key, 0)
    cursors: dict[str, int] = state.setdefault("cursors", {})
    # document id -> [selector hash, bucket start, content fingerprint]
    buckets: dict[str, list] = state.setdefault("buckets", {})
    # only whole buckets are read, so a bucket's document never misses its newest lines
    end = now // bucket * bucket
    window = max(bucket, query_window // bucket * bucket)
    active = {_hash(selector) for selector in selectors}

    for selector in selectors:
        if stats["cut_short"]:
            break
        selector_id = _hash(selector)
        cursor = cursors.get(selector_id)
        if cursor is None:
            start = (now - lookback) // bucket * bucket
        else:
            start = max(cursor - overlap_buckets * bucket, 0)
        for window_start, window_end in _windows(start, end, window):
            entries = fetch_window(
                client, selector, window_start, window_end, page_limit=page_limit
            )
            grouped: dict[tuple[str, int], tuple[dict, list]] = {}
            for ts, stream_key, labels, line, metadata in entries:
                slot = grouped.setdefault((stream_key, ts // bucket * bucket), (labels, []))
                slot[1].append((ts, line, metadata))
            for (_, bucket_start), (labels, lines) in sorted(grouped.items()):
                lines.sort(key=lambda item: item[0])
                if keep_for is not None and bucket_start < now - keep_for:
                    continue  # already past retention, don't ingest it only to forget it
                row = render_bucket(
                    selector,
                    labels,
                    bucket_start,
                    bucket_start + bucket,
                    lines,
                    max_lines=max_lines_per_bucket,
                )
                fingerprint = _hash(row["content"])
                stats["lines"] += len(lines)
                known = buckets.get(row["id"])
                if known and known[2] == fingerprint:
                    stats["skipped"] += 1
                    continue
                buckets[row["id"]] = [selector_id, bucket_start, fingerprint]
                stats["emitted"] += 1
                yield row
            cursors[selector_id] = max(cursors.get(selector_id, 0), window_end)
            if stats["lines"] >= max_lines and window_end < end:
                stats["cut_short"] = 1
                logger.warning(
                    "Loki sync read %d lines and stopped early; it resumes on the next run. "
                    "Narrow the selectors or raise max_lines.",
                    stats["lines"],
                )
                break

    # selectors no longer configured are forgotten, and so is anything past keep_for
    for selector_id in list(cursors):
        if selector_id not in active:
            del cursors[selector_id]
    for row_id, (selector_id, bucket_start, _) in sorted(buckets.items()):
        expired = keep_for is not None and bucket_start < now - keep_for
        if selector_id in active and not expired:
            continue
        del buckets[row_id]
        stats["deleted"] += 1
        yield {"id": row_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def loki_source(
    *,
    selectors: list[str],
    resource_name: str,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    token: str | None = None,
    tenant_id: str | None = None,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    keep_for_days: float | None = DEFAULT_KEEP_FOR_DAYS,
    query_window_minutes: int = DEFAULT_QUERY_WINDOW_MINUTES,
    overlap_buckets: int = DEFAULT_OVERLAP_BUCKETS,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_lines: int = DEFAULT_MAX_LINES,
    max_lines_per_bucket: int = DEFAULT_MAX_LINES_PER_BUCKET,
    clock: Callable[[], int] = time.time_ns,
):
    """Return a ``dlt`` resource that yields Loki log buckets as documents for ``remember``.

    Args:
        selectors: LogQL log queries, for example ``['{service_name="checkout"} |= "error"']``.
            Each keeps its own cursor; removing one forgets the documents it produced.
        resource_name: Stable name of this connection. It scopes the sync state and the
            cleanup of expired documents, so keep it fixed across runs and give every
            Loki instance its own.
        base_url: Loki's address, for example ``http://localhost:3100`` or a Grafana Cloud
            Loki URL. Used to build the client when ``service`` is omitted.
        username, password: Basic auth, as Grafana Cloud uses (user id and access token).
        token: Bearer token, for a Loki behind an auth proxy.
        tenant_id: Sent as ``X-Scope-OrgID`` to a multi-tenant Loki.
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each row.
        bucket_minutes: Length of the window one document covers, per stream.
        lookback_hours: How far back the first run reads.
        keep_for_days: Documents whose window started longer ago are forgotten. ``None``
            keeps them; set it at or below Loki's retention so memory never outlives the
            logs it was built from.
        query_window_minutes: Time span of one request, kept under Loki's query limits.
        overlap_buckets: Buckets read again on each run, to pick up late lines.
        page_limit: Lines per request; Loki's default ``max_entries_limit_per_query`` is 5000.
        max_lines: Line budget of one run. A bigger backfill finishes over several runs.
        max_lines_per_bucket: Lines kept in one document; the rest are counted, not shown.
        clock: Current time in nanoseconds, for tests.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Loki sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("loki_source needs a resource_name, one per Loki instance")
    selectors = [s.strip() for s in selectors or [] if isinstance(s, str) and s.strip()]
    if not selectors:
        raise ValueError("loki_source needs at least one LogQL selector")
    if len(set(selectors)) != len(selectors):
        raise ValueError("loki_source got the same selector twice")
    if service is None and not base_url:
        raise ValueError("loki_source needs a service or a base_url")
    if bucket_minutes < 1 or query_window_minutes < 1 or lookback_hours <= 0:
        raise ValueError("bucket_minutes, query_window_minutes and lookback_hours must be positive")
    if keep_for_days is not None and keep_for_days <= 0:
        raise ValueError("keep_for_days must be positive, or None to keep documents")
    if page_limit < 1 or max_lines < 1 or max_lines_per_bucket < 1 or overlap_buckets < 0:
        raise ValueError("page_limit, max_lines and max_lines_per_bucket must be positive")

    # built here so bad settings fail at construction and the closure holds no raw secret
    client = (
        service
        if service is not None
        else LokiClient(
            base_url or "",
            username=username,
            password=password,
            token=token,
            tenant_id=tenant_id,
        )
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def loki_logs():
        stats.clear()
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            selectors=selectors,
            now=clock(),
            bucket=bucket_minutes * _NS_PER_MINUTE,
            lookback=int(lookback_hours * _NS_PER_HOUR),
            keep_for=None if keep_for_days is None else int(keep_for_days * _NS_PER_DAY),
            query_window=query_window_minutes * _NS_PER_MINUTE,
            overlap_buckets=overlap_buckets,
            page_limit=page_limit,
            max_lines=max_lines,
            max_lines_per_bucket=max_lines_per_bucket,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = loki_logs()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "loki")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
