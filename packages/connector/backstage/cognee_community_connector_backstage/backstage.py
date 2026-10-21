"""Backstage connector for cognee — a ``dlt`` source that turns a software catalog into memory.

Sync a Backstage catalog (components, APIs, systems, domains, resources, groups
and users, plus opt-in TechDocs pages) into cognee, incrementally and with
forget-on-delete::

    import cognee
    from cognee_community_connector_backstage import backstage_source

    await cognee.remember(
        backstage_source(base_url="https://backstage.example.com", token="..."),
        dataset_name="backstage",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**. cognee applies the caller's
   write disposition to every table of the source; this source only emits
   documents that changed and deletes by emitting ``_deleted`` tombstone rows,
   and both only work under ``merge``.

Design
------
* **Auth** — the backend's base URL plus an optional static token
  (``backend.auth.externalAccess``), sent as ``Authorization: Bearer``.
  Instances that allow guest reads, such as https://demo.backstage.io, need no
  token. The connector only reads.
* **What is synced** — one document per catalog entity of the selected kinds
  (default: Component, API, System, Domain, Resource, Group, User) and
  namespaces. ``Location`` and ``Template`` entities are catalog plumbing and
  are left out unless asked for. Each document holds the entity's description,
  type, lifecycle, tags, links, an allowlist of annotations, and its relations
  written as sentences ("queue-proxy is owned by the group team-b"), so cognify
  extracts the ownership and dependency edges. Relation targets are named by
  their title or display name when the target is in the synced catalog.
* **Incremental sync** — every run lists the selected entities through
  ``/api/catalog/entities/by-query`` (cursor paging, server-side filters) and
  renders them. A hash of each rendered document is kept in dlt resource state,
  so only documents that changed are emitted. That includes an entity whose
  relation target was renamed, because its text changes too.
* **Forget-on-delete** — an entity missing from the listing (deleted, or no
  longer in the selected kinds or namespaces) is emitted with the ``_deleted``
  hard-delete marker; dlt drops it on ``merge`` and cognee's orphan cleanup
  removes it from the graph and vector stores. Any failed request, or a cursor
  that repeats, aborts the run before tombstones are emitted, so a partial listing
  never looks like a deletion. An empty listing for the same selection as the last
  run (what a token that lost its read permission looks like) stops the run
  instead of forgetting everything.
* **Host hooks** — ``check_active`` is called before every request and every row,
  and ``source.cognee_sync_stats`` reports counts only, like cognee's own
  connectors.
* **TechDocs (opt-in)** — pass ``techdocs=["component:default/my-service", ...]``
  to add one document per TechDocs page of those entities, read from the
  site's search index. The small ``techdocs_metadata.json`` is checked on
  every run and the index is downloaded again only when its build timestamp
  changes; pages that disappear, and entities dropped from ``techdocs``, are
  tombstoned.

Limitations
-----------
* Each run lists every selected entity (the catalog has no change feed), so
  narrow very large catalogs with ``kinds`` and ``namespaces``.
* API definitions (``spec.definition``) are not rendered; they are often large
  OpenAPI documents. Annotations are rendered only when allowlisted.
"""

import hashlib
import html
import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import quote

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("backstage_connector")

BACKSTAGE_SOURCE_NAME = "backstage"
DOCUMENTS_TABLE_NAME = "backstage_documents"

DEFAULT_KINDS: tuple[str, ...] = (
    "Component",
    "API",
    "System",
    "Domain",
    "Resource",
    "Group",
    "User",
)

# Annotations worth remembering. Most others are internal plumbing (managed-by
# locations, edit URLs, integration ids), so they are left out unless allowlisted.
DEFAULT_ANNOTATIONS: tuple[str, ...] = (
    "github.com/project-slug",
    "backstage.io/kubernetes-id",
    "pagerduty.com/service-id",
    "jira/project-key",
)

# How each relation type reads in a sentence: "<subject> <phrase> the <kind> <target>".
_RELATION_PHRASES = {
    "ownedBy": "is owned by",
    "ownerOf": "owns",
    "partOf": "is part of",
    "hasPart": "includes",
    "providesApi": "provides",
    "apiProvidedBy": "is provided by",
    "consumesApi": "consumes",
    "apiConsumedBy": "is consumed by",
    "dependsOn": "depends on",
    "dependencyOf": "is a dependency of",
    "memberOf": "is a member of",
    "hasMember": "includes",
    "childOf": "is a sub-group of",
    "parentOf": "is the parent group of",
}

_SPEC_FIELDS = (("type", "Type"), ("lifecycle", "Lifecycle"))
_LIST_FIELDS = (
    "kind,metadata.name,metadata.namespace,metadata.title,metadata.description,"
    "metadata.tags,metadata.links,metadata.annotations,spec.type,spec.lifecycle,"
    "spec.profile,relations"
)
_PAGE_SIZE = 500
_MAX_PAGES = 10_000
_MAX_RETRIES = 5
_MAX_RETRY_DELAY_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 60

_TOMBSTONE_COLUMNS = {"_deleted": {"data_type": "bool", "hard_delete": True}}

_BLOCK_END_RE = re.compile(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|blockquote|pre)>")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\r\f\v\xa0]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class BackstageNotFoundError(LookupError):
    """The requested Backstage path does not exist (HTTP 404)."""


class BackstageSyncError(RuntimeError):
    """The run cannot be trusted (stuck paging, an empty catalog, a bad response) and stops."""


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def backstage_source(
    base_url: str | None = None,
    token: str | None = None,
    kinds: Sequence[str] = DEFAULT_KINDS,
    namespaces: Sequence[str] | None = None,
    techdocs: Sequence[str] = (),
    annotations: Sequence[str] = DEFAULT_ANNOTATIONS,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    session: Any = None,
):
    """Create a dlt source that yields Backstage catalog entities as documents.

    Args:
        base_url: The Backstage backend, e.g. ``https://backstage.example.com``.
            Falls back to ``BACKSTAGE_BASE_URL``.
        token: Static external-access token, sent as ``Authorization: Bearer``.
            Falls back to ``BACKSTAGE_TOKEN``; omit it for guest-readable instances.
        kinds: Entity kinds to sync. Defaults to :data:`DEFAULT_KINDS`.
        namespaces: Namespaces to sync; ``None`` means every namespace.
        techdocs: Entity refs (``kind:namespace/name``) whose TechDocs pages are
            synced too.
        annotations: Annotation keys rendered into documents.
        check_active: Optional host checkpoint, called before every request and before
            every row is published; raising from it stops the run.
        service: Pre-built ``requests``-compatible session (tests, proxies). It is
            used as is and not closed; when omitted, each run opens and closes its own.
        session: Alias of ``service``. Give at most one.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. After a
        run, ``source.cognee_sync_stats`` holds counts only: ``scanned``, ``skipped``
        (unchanged), ``changed``, ``deleted``, ``failed`` (malformed entities),
        ``techdocs_pages``, ``requests``, and ``complete`` (1 once the run finished).
    """
    base = (base_url or os.environ.get("BACKSTAGE_BASE_URL") or "").rstrip("/")
    if not base:
        raise ValueError("Backstage URL required: pass base_url= or set BACKSTAGE_BASE_URL.")
    if not kinds:
        raise ValueError("kinds must name at least one entity kind.")
    docs_refs = sorted({_parse_ref(ref)[3] for ref in techdocs})
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both.")
    client = service if service is not None else session
    secret = _check_token(token if token is not None else os.environ.get("BACKSTAGE_TOKEN"))
    stats: dict[str, int] = {}
    config = SyncConfig(
        kinds=tuple(kinds),
        namespaces=tuple(namespaces or ()),
        techdocs=tuple(docs_refs),
        annotations=tuple(annotations),
    )

    @dlt.resource(
        name=DOCUMENTS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def backstage_documents():
        http = client if client is not None else _make_session(secret)
        try:
            api = BackstageAPI(http, base, check_active=check_active, stats=stats)
            state = dlt.current.resource_state()
            yield from sync_catalog(api, config, state, stats=stats, check_active=check_active)
        finally:
            if client is None:
                http.close()

    @dlt.source(name=BACKSTAGE_SOURCE_NAME)
    def _backstage():
        return backstage_documents

    source = _backstage()
    # Opt into the document ingestion path (row → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, BACKSTAGE_SOURCE_NAME)
    # One dlt state (document hashes, TechDocs builds) per instance and cognee
    # dataset, so syncing other sources or datasets on the machine can't reset it.
    setattr(source, PIPELINE_SCOPE_ATTR, f"{BACKSTAGE_SOURCE_NAME}:{base}")
    source.cognee_sync_stats = stats
    return source


class SyncConfig:
    """What to sync: kinds, namespaces, TechDocs entity refs and annotation allowlist."""

    def __init__(
        self,
        kinds: Sequence[str] = DEFAULT_KINDS,
        namespaces: Sequence[str] = (),
        techdocs: Sequence[str] = (),
        annotations: Sequence[str] = DEFAULT_ANNOTATIONS,
    ):
        self.kinds = tuple(kinds)
        self.namespaces = tuple(namespaces)
        self.techdocs = tuple(techdocs)
        self.annotations = tuple(annotations)


def _check_token(token: str | None) -> str:
    """Strip a pasted token and reject a malformed one without echoing it."""
    secret = (token or "").strip()
    if secret and (not secret.isascii() or not secret.isprintable() or " " in secret):
        raise ValueError("The Backstage token contains spaces or control characters.")
    return secret


def _make_session(token: str | None) -> Any:
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class BackstageAPI:
    """Thin read-only client for the catalog and TechDocs APIs: retries and paging."""

    def __init__(
        self,
        session: Any,
        base_url: str,
        check_active: Callable[[], None] | None = None,
        stats: dict | None = None,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.check_active = check_active
        self.stats = stats if stats is not None else {}

    def get(self, path: str, params: Any = None) -> Any:
        """GET a backend path, retrying 429/5xx and network errors; others raise."""
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            if self.check_active is not None:
                self.check_active()
            self.stats["requests"] = self.stats.get("requests", 0) + 1
            try:
                response = self.session.request(
                    "GET", url, params=params, timeout=_REQUEST_TIMEOUT_SECONDS
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                reason, delay = str(exc), min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)
            else:
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == _MAX_RETRIES - 1:
                    return _parse(response, path)
                reason = f"HTTP {response.status_code}"
                delay = _retry_delay(response.headers, attempt)
            logger.warning("Backstage: %s on %s — retrying in %.1fs.", reason, path, delay)
            time.sleep(delay)
            attempt += 1

    def entities(self, filters: Sequence[str]) -> Iterator[dict]:
        """Yield every entity matching any of ``filters``, following the cursor."""
        params: list[tuple[str, Any]] = [("filter", f) for f in filters]
        params += [("fields", _LIST_FIELDS), ("limit", _PAGE_SIZE)]
        cursor = None
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            # The cursor carries the filters but not the field selection.
            page_params = (
                [("cursor", cursor), ("fields", _LIST_FIELDS), ("limit", _PAGE_SIZE)]
                if cursor
                else params
            )
            page = self.get("/api/catalog/entities/by-query", page_params)
            yield from page.get("items") or []
            cursor = (page.get("pageInfo") or {}).get("nextCursor")
            if not cursor:
                return
            if cursor in seen:
                # A listing that cannot move forward must not look finished.
                raise BackstageSyncError("Backstage returned the same page cursor twice.")
            seen.add(cursor)
        raise BackstageSyncError(f"Backstage listing exceeded {_MAX_PAGES} pages.")


def _parse(response: Any, path: str) -> Any:
    """Raise on a failed response with a hint for the common causes; else decode JSON."""
    if response.status_code in (401, 403):
        raise requests.HTTPError(
            f"Backstage refused {path} (HTTP {response.status_code}). Pass token= with a "
            "static external-access token (backend.auth.externalAccess) that can read the "
            "catalog, or enable guest access.",
            response=response,
        )
    if response.status_code == 404:
        raise BackstageNotFoundError(f"Backstage has nothing at {path} (HTTP 404).")
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise BackstageSyncError(
            f"Backstage answered {path} with a web page instead of JSON (HTTP "
            f"{response.status_code}). base_url should point at the Backstage backend."
        ) from None


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    retry_after = (headers or {}).get("Retry-After")
    if retry_after is not None:
        try:
            return min(float(retry_after), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def catalog_filters(kinds: Sequence[str], namespaces: Sequence[str]) -> list[str]:
    """Server-side filters: one per kind (and namespace); Backstage ORs repeated filters."""
    if not namespaces:
        return [f"kind={kind}" for kind in kinds]
    return [f"kind={kind},metadata.namespace={ns}" for kind in kinds for ns in namespaces]


def sync_catalog(
    api: BackstageAPI,
    config: SyncConfig,
    state: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed entity documents and their tombstones, then the TechDocs pages."""
    if stats is None:
        stats = api.stats
    api.stats = stats  # requests are counted in the same dictionary
    stats.clear()
    stats.update(scanned=0, skipped=0, changed=0, deleted=0, failed=0, techdocs_pages=0)
    stats.update(requests=0, complete=0)

    # Read everything before yielding anything: a failed page aborts the run here,
    # before a partial listing could turn into tombstones.
    entities = []
    for entity in api.entities(catalog_filters(config.kinds, config.namespaces)):
        if _well_formed(entity):
            entities.append(entity)
        else:
            stats["failed"] += 1
            logger.warning("Backstage: skipped an entity without a kind or metadata.name.")
    titles = {entity_ref(entity): display_name(entity) for entity in entities}
    rows = {}
    for entity in entities:
        row = entity_to_row(api.base_url, entity, titles, config.annotations)
        rows[row["id"]] = row

    known: dict = state.setdefault("entities", {})
    selection = [sorted(k.lower() for k in config.kinds), sorted(config.namespaces)]
    if not rows and known and state.get("selection") == selection:
        # Same selection as last time but nothing came back: more likely a token
        # that lost its read permission than an emptied catalog. Forget nothing.
        raise BackstageSyncError(
            f"Backstage listed no entities for the same selection as the last run, "
            f"which synced {len(known)}. Refusing to forget them; check the token's "
            "catalog read permission."
        )
    current = {doc_id: _digest(row) for doc_id, row in rows.items()}
    changed = sorted(doc_id for doc_id, digest in current.items() if known.get(doc_id) != digest)
    deleted = sorted(set(known) - set(current))
    # Resolve TechDocs before emitting, so its errors also abort the run up front.
    docs_rows = list(_sync_techdocs(api, config.techdocs, state.setdefault("techdocs", {})))

    outgoing = [rows[doc_id] for doc_id in changed]
    outgoing += [{"id": doc_id, "_deleted": True} for doc_id in deleted]
    for row in outgoing + docs_rows:
        if check_active is not None:
            check_active()
        yield row

    known.clear()
    known.update(current)
    state["selection"] = selection
    stats.update(
        scanned=len(current),
        skipped=len(current) - len(changed),
        changed=len(changed),
        deleted=len(deleted),
        techdocs_pages=sum(1 for row in docs_rows if not row.get("_deleted")),
        complete=1,
    )
    logger.info(
        "Backstage: %d entities listed — %d changed, %d deletion(s).",
        len(current),
        len(changed),
        len(deleted),
    )


def _sync_techdocs(api: BackstageAPI, refs: Sequence[str], known: dict) -> Iterator[dict]:
    """TechDocs pages of ``refs`` that changed since the last build seen, plus tombstones."""
    for ref in sorted(set(known) - set(refs)):
        # The entity was dropped from ``techdocs``: forget its pages.
        for page_id in known.pop(ref)["pages"]:
            yield {"id": page_id, "_deleted": True}
    for ref in refs:
        kind, namespace, name, _ = _parse_ref(ref)
        site = f"/api/techdocs/static/docs/{_path(namespace)}/{_path(kind)}/{_path(name)}"
        previous = known.get(ref, {"build": None, "pages": []})
        try:
            metadata = api.get(f"{site}/techdocs_metadata.json")
        except BackstageNotFoundError:
            logger.warning("Backstage: no TechDocs for %s; forgetting its pages.", ref)
            for page_id in previous["pages"]:
                yield {"id": page_id, "_deleted": True}
            known.pop(ref, None)
            continue
        build = str(metadata.get("build_timestamp") or metadata.get("etag") or "")
        if build and build == previous["build"]:
            continue
        index = api.get(f"{site}/search/search_index.json")
        site_name = str(metadata.get("site_name") or name)
        pages = techdocs_pages(api.base_url, ref, site_name, index)
        yield from pages
        for page_id in sorted(set(previous["pages"]) - {page["id"] for page in pages}):
            yield {"id": page_id, "_deleted": True}
        known[ref] = {"build": build, "pages": sorted(page["id"] for page in pages)}
        logger.info("Backstage: %s TechDocs — %d page(s) synced.", ref, len(pages))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _well_formed(entity: Any) -> bool:
    metadata = entity.get("metadata") if isinstance(entity, dict) else None
    return bool(isinstance(metadata, dict) and entity.get("kind") and metadata.get("name"))


def entity_ref(entity: Mapping[str, Any]) -> str:
    """Backstage's canonical entity ref: ``kind:namespace/name``, lowercased."""
    metadata = entity.get("metadata") or {}
    namespace = metadata.get("namespace") or "default"
    return f"{entity['kind']}:{namespace}/{metadata['name']}".lower()


def display_name(entity: Mapping[str, Any]) -> str:
    metadata = entity.get("metadata") or {}
    profile = (entity.get("spec") or {}).get("profile") or {}
    return str(metadata.get("title") or profile.get("displayName") or metadata["name"]).strip()


def entity_to_row(
    base_url: str,
    entity: Mapping[str, Any],
    titles: Mapping[str, str],
    annotations: Sequence[str] = DEFAULT_ANNOTATIONS,
) -> dict:
    """Render one entity, with its relations as sentences, as a document row."""
    metadata = entity.get("metadata") or {}
    spec = entity.get("spec") or {}
    kind = str(entity["kind"])
    name = str(metadata["name"])
    namespace = str(metadata.get("namespace") or "default")
    title = display_name(entity)

    heading = f"Backstage {_kind_label(kind)} {name}"
    if title != name:
        heading += f" ({title})"
    if namespace != "default":
        heading += f" in namespace {namespace}"
    lines = [heading + "."]
    description = _plain(metadata.get("description"))
    if description:
        lines.append(description)
    for field, label in _SPEC_FIELDS:
        if spec.get(field):
            lines.append(f"{label}: {spec[field]}")
    profile = spec.get("profile") or {}
    if profile.get("email"):
        lines.append(f"Email: {profile['email']}")
    tags = [str(tag) for tag in metadata.get("tags") or []]
    if tags:
        lines.append("Tags: " + ", ".join(tags))
    for link in metadata.get("links") or []:
        if link.get("url"):
            label = link.get("title") or link.get("type") or "Link"
            lines.append(f"Link: {label} — {link['url']}")
    entity_annotations = metadata.get("annotations") or {}
    for key in annotations:
        if entity_annotations.get(key):
            lines.append(f"{key}: {entity_annotations[key]}")
    relations = sorted(
        (str(rel.get("type")), str(rel.get("targetRef") or "").lower())
        for rel in entity.get("relations") or []
        if rel.get("type") and rel.get("targetRef")
    )
    for relation, target in relations:
        lines.append(_relation_sentence(title, relation, target, titles))

    return {
        "id": f"entity:{entity_ref(entity)}",
        "title": f"{_kind_label(kind, title_case=True)}: {title}",
        "content": "\n".join(lines),
        "url": f"{base_url}/catalog/{_path(namespace)}/{_path(kind.lower())}/{_path(name)}",
        "_deleted": False,
    }


def techdocs_pages(base_url: str, ref: str, site_name: str, index: Mapping[str, Any]) -> list[dict]:
    """Group a TechDocs (MkDocs) search index into one document row per page."""
    kind, namespace, name, ref = _parse_ref(ref)
    pages: dict[str, dict] = {}
    for entry in index.get("docs") or []:
        location = str(entry.get("location") or "")
        page = location.split("#", 1)[0]
        section_title = _plain(entry.get("title"))
        text = _plain(entry.get("text"))
        doc = pages.setdefault(page, {"title": "", "sections": []})
        if "#" not in location and section_title and not doc["title"]:
            doc["title"] = section_title
        if section_title and section_title != doc["title"]:
            doc["sections"].append(section_title)
        if text:
            doc["sections"].append(text)
    rows = []
    for page, doc in sorted(pages.items()):
        page_title = doc["title"] or page.strip("/") or "Home"
        body = "\n".join(doc["sections"])
        if not body:
            continue
        rows.append(
            {
                "id": f"techdocs:{ref}/{page}",
                "title": f"{site_name}: {page_title}",
                "content": f"TechDocs for {name} ({site_name}), page {page_title}.\n{body}",
                "url": f"{base_url}/docs/{_path(namespace)}/{_path(kind)}/{_path(name)}/{page}",
                "_deleted": False,
            }
        )
    return rows


def _relation_sentence(subject: str, relation: str, target: str, titles: Mapping[str, str]) -> str:
    kind, namespace, name, ref = _parse_ref(target)
    target_name = titles.get(ref, name)
    where = "" if namespace == "default" else f" in namespace {namespace}"
    phrase = _RELATION_PHRASES.get(relation)
    if phrase is None:
        return (
            f"{subject} has a {relation} relation to the {_kind_label(kind)} {target_name}{where}."
        )
    return f"{subject} {phrase} the {_kind_label(kind)} {target_name}{where}."


def _parse_ref(ref: str) -> tuple[str, str, str, str]:
    """Split ``kind:namespace/name`` (namespace optional) into parts and a normalized ref."""
    kind, sep, rest = str(ref).partition(":")
    if not sep or not rest:
        raise ValueError(f"Entity ref {ref!r} must look like kind:namespace/name.")
    namespace, _, name = rest.rpartition("/")
    kind, namespace, name = kind.lower(), (namespace or "default").lower(), name.lower()
    return kind, namespace, name, f"{kind}:{namespace}/{name}"


def _kind_label(kind: str, title_case: bool = False) -> str:
    if kind.lower() == "api":
        return "API"
    words = _CAMEL_RE.sub(" ", kind).lower()
    return words.title() if title_case else words


def _path(segment: str) -> str:
    return quote(str(segment), safe="")


def _plain(value: Any) -> str:
    """Render text that may contain HTML (TechDocs, descriptions) as plain text."""
    if not value:
        return ""
    text = _BLOCK_END_RE.sub("\n", str(value))
    text = html.unescape(_TAG_RE.sub("", text)).replace("¶", "")
    lines = (_SPACES_RE.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _digest(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
