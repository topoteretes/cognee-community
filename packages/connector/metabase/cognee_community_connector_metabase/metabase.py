"""Metabase documents: incremental rendering with authoritative snapshot cleanup.

Like Notion, staging uses replace and cognee's document-source marker. Cached
rows keep unchanged documents in the snapshot so orphan_cleanup only forgets
items that actually disappeared. Cursors and rows live in dlt source state.
"""

import json
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("metabase_connector")

METABASE_TABLE_NAME = "metabase_documents"
METABASE_SOURCE_NAME = "metabase"
_MAX_RETRIES = 5
_KINDS = ("collection", "card", "dashboard")


class MetabaseUnchanged(Exception):  # noqa: N818 - a no-op signal, not an error
    """No upstream changes; abort replace extraction without touching staging.

    dlt/cognee wrap extraction exceptions. Inspect the exception's cause chain
    for this signal, as shown in examples/example.py.
    """


def metabase_source(
    base_url: str | None = None,
    api_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
    *,
    kinds: list[str] | None = None,
    client: Any = None,
    request_interval: float = 0.1,
):
    """Create a document-mode dlt source for a self-hosted Metabase instance.

    Credentials default to METABASE_API_KEY or METABASE_USERNAME/PASSWORD;
    base_url defaults to METABASE_URL. An API key takes precedence. ``kinds``
    selects collection/card/dashboard (all by default). Keep the same dataset
    and dlt pipeline storage across runs to retain incremental state. A supplied
    httpx client is caller-owned; the connector still supplies auth headers.
    """
    import dlt
    import httpx

    base_url = (base_url or os.environ.get("METABASE_URL", "")).rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Pass base_url= or set METABASE_URL to your instance URL.")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Metabase base URL must not contain credentials, a query, or a fragment.")
    api_key = api_key or os.environ.get("METABASE_API_KEY")
    username = username or os.environ.get("METABASE_USERNAME")
    password = password or os.environ.get("METABASE_PASSWORD")
    if not api_key and not (username and password):
        raise ValueError("Metabase API key or username and password required.")
    selected = tuple(sorted(set(_KINDS if kinds is None else kinds)))
    if not selected or set(selected) - set(_KINDS):
        raise ValueError("kinds must select collection, card, and/or dashboard.")
    if request_interval < 0:
        raise ValueError("request_interval must be nonnegative.")

    @dlt.resource(name=METABASE_TABLE_NAME, primary_key="id", write_disposition="replace")
    def metabase_documents():
        session = client if client is not None else httpx.Client(timeout=30)
        api = _API(session, base_url, request_interval)
        try:
            if api_key:
                api.headers["x-api-key"] = api_key
            else:
                token = api.request(
                    "POST", "/api/session", json={"username": username, "password": password}
                )["id"]
                if not token:
                    raise ValueError("Metabase returned an empty session token.")
                api.headers["X-Metabase-Session"] = token

            # dlt resets resource state before every replace extraction. Keep
            # the incremental cache in source state so snapshots retain it.
            state = dlt.current.source_state().setdefault("metabase_snapshot", {})
            snapshot = _sync(api, state, selected)
            if snapshot is None:
                logger.info("Metabase: unchanged; skipping snapshot load.")
                # Returning an empty iterator under replace truncates staging.
                raise MetabaseUnchanged("Metabase content is unchanged.")
            yield snapshot
        finally:
            # Release the one session created for this run; never accumulate
            # sessions toward Metabase's concurrent-session limit.
            if not api_key and api.headers.get("X-Metabase-Session"):
                try:
                    api.request("DELETE", "/api/session")
                except (httpx.HTTPError, ValueError):
                    logger.warning("Metabase: session logout failed.")
            if client is None:
                session.close()

    @dlt.source(name=METABASE_SOURCE_NAME)
    def _metabase():
        return metabase_documents

    source = _metabase()
    setattr(source, DOCUMENT_SOURCE_ATTR, METABASE_SOURCE_NAME)
    return source


class _API:
    """Sequential, paced HTTP calls; bounded retries on transient failures."""

    def __init__(self, client, base_url, interval):
        self.client = client
        self.base_url = base_url
        self.interval = interval
        self.headers = {"Accept": "application/json"}

    def request(self, method, path, **kwargs):
        import httpx

        for attempt in range(_MAX_RETRIES):
            time.sleep(self.interval)
            try:
                response = self.client.request(
                    method, self.base_url + path, headers=self.headers, **kwargs
                )
                response.raise_for_status()
                return response.json() if response.content else None
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                response = getattr(exc, "response", None)
                if attempt == _MAX_RETRIES - 1 or (
                    response is not None and response.status_code not in (429, 500, 502, 503, 504)
                ):
                    raise
                try:
                    delay = max(0, float(response.headers.get("Retry-After", "")))
                except (AttributeError, TypeError, ValueError):
                    delay = float(2**attempt)
                logger.warning("Metabase: transient request failure; retrying in %.1fs.", delay)
                time.sleep(delay)

    def listing(self, kind):
        offset = 0
        while True:
            payload = self.request("GET", f"/api/{kind}", params={"limit": 100, "offset": offset})
            # Older versions return an unpaginated array; newer endpoints can
            # return an envelope with data/total/offset/limit.
            if isinstance(payload, list):
                yield from payload
                return
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError(f"Invalid Metabase {kind} listing; refusing partial snapshot.")
            items = payload["data"]
            total = int(payload["total"])
            if int(payload.get("offset", offset)) != offset:
                raise ValueError("Metabase pagination did not advance.")
            yield from items
            offset += len(items)
            if offset >= total:
                return
            if not items:
                raise ValueError("Incomplete Metabase listing; refusing partial snapshot.")


def _watermark(item):
    value = item.get("updated_at")
    if not value:
        # Some versions omit collection timestamps (including the virtual
        # root collection). Re-render these; content hashes still deduplicate.
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _sync(api, state, kinds):
    """Return an authoritative snapshot, or None for an unchanged sync.

    Per-ID watermarks avoid losing late-arriving edits below a global maximum.
    State is replaced only after all listings and detail requests succeed;
    dlt commits it with the load. Auth/list/render failures cannot drive purge.
    """
    scope = {"base_url": api.base_url, "kinds": list(kinds)}
    previous = state.get("documents", {}) if state.get("scope") == scope else {}
    current = {}
    for kind in kinds:
        for item in api.listing(kind):
            if item.get("archived"):
                continue
            key = f"{kind}:{item['id']}"
            when = _watermark(item)
            old = previous.get(key)
            if old and when is not None and old["updated_at"] == when:
                current[key] = old
                continue
            # Listings omit dashboard cards and may omit question definitions.
            if kind in ("card", "dashboard"):
                item = api.request("GET", f"/api/{kind}/{item['id']}")
                if item.get("archived"):
                    continue
            current[key] = {
                "updated_at": when,
                "row": _item_to_row(kind, item, api.base_url),
            }

    if current == previous and state.get("scope") == scope:
        return None
    deleted = set(previous) - set(current)
    state["scope"] = scope
    state["documents"] = current
    state["cursor"] = max(
        (entry["updated_at"] for entry in current.values() if entry["updated_at"]),
        default=None,
    )
    logger.info("Metabase: synced %d document(s), %d deletion(s).", len(current), len(deleted))
    # cognee 1.4.0 skips orphan cleanup for an empty read-back. A stable source
    # document keeps the snapshot authoritative even after the final deletion.
    manifest = {
        "id": "metabase:source",
        "url": api.base_url,
        "title": "Metabase source",
        "content": f"Metabase knowledge source: {api.base_url}",
    }
    return [manifest, *(current[key]["row"] for key in sorted(current))]


def _item_to_row(kind, item, base_url):
    """Stable identity/provenance and markdown, without volatile timestamps."""
    title = item.get("name") or f"{kind.title()} {item['id']}"
    lines = [f"Type: {kind}", item.get("description") or ""]
    if kind == "card":
        definition = item.get("dataset_query") or {}
        lines.extend(
            [
                "## Question definition",
                "```json",
                json.dumps(definition, indent=2, sort_keys=True, ensure_ascii=False),
                "```",
            ]
        )
        query = (definition.get("native") or {}).get("query")
        if query:
            lines.extend(["## Query", "```sql", query, "```"])
    elif kind == "dashboard":
        lines.append("## Dashboard cards")
        for dashcard in item.get("dashcards", item.get("ordered_cards", [])) or []:
            card = dashcard.get("card") or {}
            card_id = card.get("id", dashcard.get("card_id"))
            lines.append(f"- {card.get('name') or 'Text card'} (card: {card_id})")
            if card.get("description"):
                lines.append(card["description"])
            text = (dashcard.get("visualization_settings") or {}).get("text")
            if text:
                lines.append(text)
    path = "question" if kind == "card" else kind
    return {
        "id": f"{kind}:{item['id']}",
        "url": f"{base_url}/{path}/{item['id']}",
        "title": title,
        "content": "\n\n".join(lines),
    }
