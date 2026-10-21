"""DLT source for Webflow CMS items and pages (lastUpdated change detection + forget-on-delete).

Syncs the CMS collections of a Webflow site, and optionally its static pages,
into cognee::

    import cognee
    from cognee_community_connector_webflow import webflow_source

    await cognee.remember(
        webflow_source(collections=["blog-posts"]),  # token from WEBFLOW_API_TOKEN
        dataset_name="webflow",
        primary_key="id",
        write_disposition="merge",  # required: the source emits deletions
    )

Design
------
* **Documents** — each CMS item becomes a short text document (a header, then
  one ``Label: value`` line per field, rich text rendered as plain text) that
  flows through normal cognify. Each collection also gets a card that lists its
  fields, their types, options and help text. Items and their card share a node
  set (``webflow:<site>/<collection>``), so they are linked in the graph. One
  staging table per collection keeps cleanup scoped to it.
* **Incremental sync** — Webflow's list endpoints return the full items, and
  deletions are only visible as items missing from a listing, so each run lists
  every selected collection (100 items per request). The connector keeps each
  item's ``lastUpdated`` and emits only items that are new or whose stamp
  changed; an unchanged item is neither re-sent nor re-read by the LLM. Pages
  save one more request each (their content), so only changed pages are read.
* **Forget-on-delete** — an item that was synced before and is missing from the
  listing is emitted as a ``_deleted`` tombstone, so dlt drops the row and
  cognee's orphan cleanup removes it from the graph and vector stores. Items
  that are archived, unpublished (default) or, with ``include_drafts``, drafts
  are out of scope and forgotten the same way. A collection or site removed
  upstream, or taken out of the selection, is forgotten whole.
* **Errors never delete** — a failed request, an unreadable page, or a listing
  that is incomplete or kept changing aborts the run before dlt commits rows or
  state, so nothing is forgotten and the next run starts from the same state.
  A suspected deletion is confirmed by reading the collection a second time.
"""

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

from .client import WebflowAPIError, WebflowClient
from .render import card_content, item_content, page_content, schema_fingerprint

logger = get_logger("webflow_connector")

WEBFLOW_SOURCE_NAME = "webflow"
DEFAULT_RESOURCE_NAME = "webflow"

# Bump when rendering or the state layout changes, so the next run re-reads
# every item instead of keeping documents rendered by an older connector.
_STATE_VERSION = 1


@dataclass(frozen=True)
class _Selection:
    """What to sync, normalized from the arguments of ``webflow_source``."""

    site_ids: tuple[str, ...] | None
    collections: tuple[str, ...] | None
    include_pages: bool
    include_drafts: bool
    include_collection_cards: bool
    full_refresh: bool
    resource_name: str


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def webflow_source(
    token: str | None = None,
    *,
    site_ids: Sequence[str] | None = None,
    collections: Sequence[str] | None = None,
    include_pages: bool = False,
    include_drafts: bool = False,
    include_collection_cards: bool = True,
    full_refresh: bool = False,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    client: Any = None,
    check_active: Callable[[], None] | None = None,
):
    """Create a dlt source that yields Webflow CMS items and pages as documents.

    Args:
        token: A Webflow site token or OAuth access token. Falls back to
            ``WEBFLOW_API_TOKEN``. Needs ``sites:read`` and ``cms:read``, plus
            ``pages:read`` for ``include_pages``.
        site_ids: Sites to sync. Defaults to every site the token can see (a
            site token sees exactly one).
        collections: Collections to sync, each given as an id, a slug or a
            display name. Defaults to every collection of the selected sites.
        include_pages: Also sync the text of static pages. Needs ``pages:read``.
            CMS template pages are skipped; their content is the CMS items.
        include_drafts: Sync the staged version of items (drafts and edits that
            are not published yet) instead of only what is published.
        include_collection_cards: Also ingest one document per collection with
            its fields, types, options and help text.
        full_refresh: Render and send every item this run, for example after
            changing how documents are rendered.
        resource_name: Stable name for this connection's dlt state and staging
            tables. Give each Webflow account its own name when several sync
            into one dataset.
        client: A ``WebflowClient`` (mainly a test-injection point); when
            omitted one is built from the token above.
        check_active: Called between rows; raises to stop a sync whose access
            was revoked mid-run (for hosts).

    Returns:
        A dlt source for ``cognee.remember(...)``. Pass
        ``write_disposition="merge"``: incremental runs emit only changes and
        tombstones, and any other disposition is refused.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Webflow connector requires a cognee build with table-scoped document "
            "cleanup and per-row node sets (DOCUMENT_SYNC_VERSION 2, cognee 1.6.4)."
        )
    if not re.fullmatch(r"[a-z][a-z0-9_]*", resource_name):
        raise ValueError("resource_name must be lower case letters, digits and underscores.")
    if isinstance(site_ids, str):
        site_ids = [site_ids]
    if isinstance(collections, str):
        collections = [collections]
    if site_ids is not None and not site_ids:
        # An empty selection would forget everything synced before.
        raise ValueError("site_ids is empty; omit it to sync every site the token can see.")
    if collections is not None and not collections:
        raise ValueError("collections is empty; omit it to sync every collection.")

    selection = _Selection(
        site_ids=tuple(site_ids) if site_ids else None,
        collections=tuple(collections) if collections else None,
        include_pages=include_pages,
        include_drafts=include_drafts,
        include_collection_cards=include_collection_cards,
        full_refresh=full_refresh,
        resource_name=resource_name,
    )
    client = client or WebflowClient(token)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the staging table on merge,
        # which is what makes cognee's orphan cleanup forget them.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}, **NODE_SET_COLUMN_HINT},
    )
    def webflow_rows():
        # The caller's write_disposition overrides the one declared above. A
        # replace run of an incremental read would empty every table but the
        # changed rows, so refuse it before reading anything.
        disposition = source.resources[resource_name].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'webflow_source needs write_disposition="merge", got {disposition!r}. '
                "Its incremental runs emit only changes and deletions."
            )
        try:
            rows = sync_rows(client, selection, dlt.current.resource_state(), stats=stats)
            for table_name, row in guarded_rows(rows, check_active):
                yield dlt.mark.with_table_name(row, table_name)
        finally:
            client.close()

    @dlt.source(name=WEBFLOW_SOURCE_NAME)
    def _webflow():
        return webflow_rows

    source = _webflow()
    # Opt into the document path (item -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, WEBFLOW_SOURCE_NAME)
    # Give the source its own dlt pipeline per dataset, so its state is not
    # shared with unrelated dlt sources.
    setattr(source, PIPELINE_SCOPE_ATTR, resource_name)
    # Counts of the last run, for hosts that record how a sync went. Never
    # content.
    source.cognee_sync_stats = stats
    return source


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_rows(
    client: Any,
    selection: _Selection,
    state: dict,
    *,
    stats: dict | None = None,
) -> Iterator[tuple[str, dict]]:
    """Yield ``(staging_table, row)`` for every changed item, card, page and tombstone.

    ``state`` stands in for dlt's resource state. It is only written after the
    last request has succeeded, and dlt only commits it together with the rows,
    so a failed run changes nothing.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(items=0, unchanged=0, deleted=0, pages=0, collections=0, skipped=0)

    name = selection.resource_name
    # A state written by another rendering is kept for its ids (so deletions
    # still work) but its stamps are not trusted.
    trusted = state.get("version") == _STATE_VERSION and not selection.full_refresh
    previous_items: dict[str, dict] = state.get("collections", {})
    previous_pages: dict[str, dict] = state.get("pages", {})
    previous_cards: set[str] = set(state.get("cards", []))
    tombstones: list[tuple[str, str]] = []

    sites = _resolve_sites(client, selection)
    collections = _resolve_collections(client, selection, sites)
    current_items: dict[str, dict] = {}
    current_cards: set[str] = set()

    for collection in collections:
        schema = client.get(f"/collections/{collection['id']}")
        known: dict[str, str] = previous_items.get(collection["id"], {}).get("stamps", {})
        table = _items_table(name, collection["id"])
        stamps = yield from _sync_collection(
            client, selection, collection, schema, known, trusted, table, tombstones, stats
        )
        current_items[collection["id"]] = {"table": table, "stamps": stamps}
        stats["collections"] += 1
        if selection.include_collection_cards:
            current_cards.add(collection["id"])
            yield (
                _cards_table(name),
                {
                    "id": _card_id(name, collection["id"]),
                    "title": f"Collection {collection['displayName']}",
                    "content": card_content(collection, schema),
                    NODE_SET_COLUMN: [collection["nodeSet"]],
                    "_deleted": False,
                },
            )

    # Collections removed upstream or from the selection: their items are out of scope.
    for collection_id, entry in previous_items.items():
        if collection_id not in current_items:
            for item_id in entry.get("stamps", {}):
                _forget(tombstones, stats, entry["table"], _item_id(name, item_id))
    for collection_id in sorted(previous_cards - current_cards):
        _forget(tombstones, stats, _cards_table(name), _card_id(name, collection_id))

    current_pages: dict[str, dict] = {}
    if selection.include_pages:
        for site in sites:
            table = _pages_table(name, site["id"])
            known_pages = previous_pages.get(site["id"], {}).get("stamps", {})
            stamps = yield from _sync_pages(
                client, selection, site, known_pages, trusted, table, tombstones, stats
            )
            current_pages[site["id"]] = {"table": table, "stamps": stamps}
    for site_id, entry in previous_pages.items():
        if site_id not in current_pages:
            for page_id in entry.get("stamps", {}):
                _forget(tombstones, stats, entry["table"], _page_id(name, page_id))

    for table, row_id in tombstones:
        yield table, {"id": row_id, "_deleted": True}

    state["version"] = _STATE_VERSION
    state["collections"] = current_items
    state["cards"] = sorted(current_cards)
    state["pages"] = current_pages
    logger.info("Webflow: sync stats %s", stats)


def _sync_collection(
    client, selection, collection, schema, known, trusted, table, tombstones, stats
):
    """Sync one collection. Yields its changed items and returns its new stamps."""
    name = selection.resource_name
    fingerprint = schema_fingerprint(schema)
    items = _read_items(client, selection, collection, stats)

    # A row created or deleted during the read can shift an offset listing and
    # hide an item. Read once more before believing an item is gone.
    suspected = set(known) - set(items)
    if suspected:
        again = _read_items(client, selection, collection, Counter())
        items.update({item_id: again[item_id] for item_id in suspected & set(again)})

    stamps: dict[str, str] = {}
    for item_id, item in items.items():
        stamp = f"{item.get('lastUpdated') or ''}|{fingerprint}"
        stamps[item_id] = stamp
        if trusted and known.get(item_id) == stamp:
            stats["unchanged"] += 1
            continue
        stats["items"] += 1
        data = item.get("fieldData") or {}
        yield (
            table,
            {
                "id": _item_id(name, item_id),
                "title": data.get("name") or data.get("slug") or item_id,
                "content": item_content(collection, schema, item),
                NODE_SET_COLUMN: [collection["nodeSet"]],
                "_deleted": False,
            },
        )
    for item_id in sorted(set(known) - set(items)):
        _forget(tombstones, stats, table, _item_id(name, item_id))
    return stamps


def _read_items(client, selection, collection, stats) -> dict[str, dict]:
    """The in-scope items of a collection by id: published, or staged with drafts."""
    path = f"/collections/{collection['id']}/items"
    if not selection.include_drafts:
        path += "/live"
    try:
        listing = client.list_all(path, "items")
    except WebflowAPIError as exc:
        if exc.status != 409 or selection.include_drafts:
            raise
        # Webflow has no live items until the site is published once. Failing
        # is the safe answer: reading it as "no items" would forget everything.
        raise WebflowAPIError(
            409,
            f"{exc.message} Publish the site in Webflow, or pass include_drafts=True to "
            "sync the staged items instead.",
            exc.code,
        ) from exc
    items: dict[str, dict] = {}
    for item in listing:
        item_id = item.get("id")
        out_of_scope = (
            not item_id
            or item.get("isArchived")
            or (item.get("isDraft") and not selection.include_drafts)
        )
        # A localized variant shares its item's id. Keep the first, so dlt's merge
        # never has to pick between two rows with one id.
        if out_of_scope or item_id in items:
            stats["skipped"] += 1
        else:
            items[item_id] = item
    return items


def _sync_pages(client, selection, site, known, trusted, table, tombstones, stats):
    """Sync one site's static pages. Yields changed pages and returns their stamps."""
    name = selection.resource_name
    listing = client.list_all(f"/sites/{site['id']}/pages", "pages")
    pages = {
        page["id"]: page
        for page in listing
        if page.get("id")
        # A CMS template page renders the items, which are synced as items.
        and not page.get("collectionId")
        and not page.get("isBranch")
        and not page.get("archived")
        and (selection.include_drafts or not page.get("draft"))
    }

    stamps: dict[str, str] = {}
    for page_id, page in pages.items():
        stamp = page.get("lastUpdated") or ""
        if trusted and known.get(page_id) == stamp:
            stamps[page_id] = stamp
            stats["unchanged"] += 1
            continue
        try:
            nodes = list(client.paginate(f"/pages/{page_id}/dom", "nodes"))
        except WebflowAPIError as exc:
            if exc.status not in (400, 403, 404):
                raise
            # Not readable on this plan or scope. Keep what was synced before;
            # an unreadable page is not a deleted one.
            logger.warning("Webflow: page %s content is unavailable, skipping: %s", page_id, exc)
            stats["skipped"] += 1
            if page_id in known:
                stamps[page_id] = known[page_id]
            continue
        stamps[page_id] = stamp
        stats["pages"] += 1
        yield (
            table,
            {
                "id": _page_id(name, page_id),
                "title": page.get("title") or page.get("slug") or page_id,
                "content": page_content(site["name"], page, nodes),
                NODE_SET_COLUMN: [site["pagesNodeSet"]],
                "_deleted": False,
            },
        )
    for page_id in sorted(set(known) - set(stamps)):
        _forget(tombstones, stats, table, _page_id(name, page_id))
    return stamps


def _forget(tombstones, stats, table: str, row_id: str) -> None:
    stats["deleted"] += 1
    tombstones.append((table, row_id))


# ---------------------------------------------------------------------------
# Sites and collections
# ---------------------------------------------------------------------------
def _resolve_sites(client, selection: _Selection) -> list[dict]:
    if selection.site_ids:
        raw = []
        for site_id in dict.fromkeys(selection.site_ids):
            try:
                raw.append(client.get(f"/sites/{site_id}"))
            except WebflowAPIError as exc:
                if exc.status == 404:
                    raise ValueError(
                        f"Webflow site {site_id} was not found, or this token cannot see it."
                    ) from exc
                if exc.status != 403:
                    raise
                # A token without sites:read can still read the site's CMS.
                raw.append({"id": site_id})
    else:
        raw = client.get("/sites").get("sites") or []
        if not raw:
            # An empty selection would forget everything synced before.
            raise ValueError(
                "This Webflow token can see no sites. Check the token and that it has the "
                "sites:read scope."
            )
    sites = []
    for site in raw:
        label = site.get("shortName") or site["id"]
        sites.append(
            {
                "id": site["id"],
                "name": site.get("displayName") or label,
                "pagesNodeSet": f"webflow:{label}/pages",
                "label": label,
            }
        )
    return sites


def _resolve_collections(client, selection: _Selection, sites: list[dict]) -> list[dict]:
    found: dict[str, dict] = {}
    for site in sites:
        listing = client.get(f"/sites/{site['id']}/collections").get("collections") or []
        for collection in listing:
            slug = collection.get("slug") or collection["id"]
            found.setdefault(
                collection["id"],
                {
                    **collection,
                    "displayName": collection.get("displayName") or collection["slug"],
                    "siteName": site["name"],
                    "nodeSet": f"webflow:{site['label']}/{slug}",
                },
            )
    if selection.collections is None:
        return list(found.values())

    chosen: dict[str, dict] = {}
    for wanted in selection.collections:
        matches = [
            collection
            for collection in found.values()
            if wanted in (collection["id"], collection.get("slug"), collection["displayName"])
        ]
        if not matches:
            # Skipping it would forget what it synced before; make the user decide.
            available = sorted(c.get("slug") or c["id"] for c in found.values())
            raise ValueError(
                f"Webflow collection {wanted!r} was not found on the selected sites. "
                f"Available: {available}. Remove it from `collections` to forget its content."
            )
        for collection in matches:
            chosen.setdefault(collection["id"], collection)
    return list(chosen.values())


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def _item_id(name: str, item_id: str) -> str:
    return f"{name}:item:{item_id}"


def _card_id(name: str, collection_id: str) -> str:
    return f"{name}:collection:{collection_id}"


def _page_id(name: str, page_id: str) -> str:
    return f"{name}:page:{page_id}"


def _table(name: str, kind: str, key: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    # Webflow ids are 24 hex characters; the hash only guards an odd one.
    return f"{name}_{kind}_{clean or hashlib.sha256(key.encode()).hexdigest()[:12]}"


def _items_table(name: str, collection_id: str) -> str:
    return _table(name, "items", collection_id)


def _pages_table(name: str, site_id: str) -> str:
    return _table(name, "pages", site_id)


def _cards_table(name: str) -> str:
    return f"{name}_collection_cards"
