"""DataHub connector for cognee: a dlt resource that syncs dataset metadata into memory.

It reads DataHub's GraphQL API (``/api/graphql``) and yields one document per
dataset, handed to ``cognee.remember``::

    await cognee.remember(
        datahub_source(base_url="http://localhost:8080", token="<personal access token>"),
        dataset_name="datahub",
        max_rows_per_table=0,
    )

Each document writes the dataset's context out as plain statements ("dataset X
is owned by Revenue Analytics", "Revenue Analytics is the Business Owner of
dataset X", "dataset raw.orders is upstream of X", "column email of X is tagged
pii") so cognify extracts them as graph relations.

Sync is a full snapshot within the chosen scope (``write_disposition="replace"``):

* soft-deleted datasets (``status.removed``) are excluded, so they drop out of
  the snapshot and cognee's orphan cleanup forgets them;
* ownership, tag, term, description, schema and lineage changes change the
  rendered document and are picked up on re-sync; unchanged datasets keep their
  content hash and are not re-processed (no usage stats, profiles or timestamps
  are rendered);
* any failed or partial response (HTTP error or GraphQL ``errors``) aborts the
  run before anything is loaded, so a dataset that is only temporarily
  unreadable is never forgotten.

DataHub's GraphQL API has no global "changed since" cursor, so there is no
incremental sync.
"""

import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("datahub_connector")

DATAHUB_SOURCE_TAG = "datahub"
# Default dlt resource / staging-table name.
DATAHUB_TABLE_NAME = "datahub_datasets"
# DataHub advises small pages for queries with nested aspects.
PAGE_SIZE = 20
# Upstream edges read per dataset (1 hop).
MAX_UPSTREAMS = 100
_TIMEOUT = (10, 60)
_RETRIES = 3

_OWNER = """owner {
  ... on CorpUser { urn username properties { displayName } }
  ... on CorpGroup { urn name properties { displayName } }
}
ownershipType { urn info { name } }
type"""

_TAGS = "tags { tag { urn properties { name } } }"
_TERMS = "terms { term { urn properties { name } } }"

DATASET_FIELDS = f"""
urn
name
origin
platform {{ name properties {{ displayName }} }}
properties {{ name qualifiedName description externalUrl }}
editableProperties {{ description }}
institutionalMemory {{ elements {{ url label }} }}
container {{ urn properties {{ name }} }}
parentContainers {{ containers {{ properties {{ name }} }} }}
schemaMetadata {{ fields {{ fieldPath nativeDataType description globalTags {{ {_TAGS} }}
  glossaryTerms {{ {_TERMS} }} }} }}
editableSchemaMetadata {{ editableSchemaFieldInfo {{ fieldPath description
  globalTags {{ {_TAGS} }} glossaryTerms {{ {_TERMS} }} }} }}
ownership {{ owners {{ {_OWNER} }} }}
domain {{ domain {{ urn properties {{ name }} }} }}
tags {{ {_TAGS} }}
glossaryTerms {{ {_TERMS} }}
status {{ removed }}
upstream: lineage(input: {{direction: UPSTREAM, start: 0, count: {MAX_UPSTREAMS}}}) {{
  total
  relationships {{ entity {{ urn ... on Dataset {{ name platform {{ name }} }} }} }}
}}
"""

SCROLL_QUERY = f"""
query scroll($input: ScrollAcrossEntitiesInput!) {{
  scrollAcrossEntities(input: $input) {{
    nextScrollId
    searchResults {{ entity {{ ... on Dataset {{ {DATASET_FIELDS} }} }} }}
  }}
}}
"""

_OWNERSHIP_TYPES = {
    "TECHNICAL_OWNER": "Technical Owner",
    "BUSINESS_OWNER": "Business Owner",
    "DATA_STEWARD": "Data Steward",
    "NONE": "Owner",
}


class DataHubSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class DataHubAuthError(DataHubSourceError):
    """DataHub rejected the token (HTTP 401/403)."""


class DataHubAPIError(DataHubSourceError):
    """DataHub answered with an error, a partial result, or could not be reached."""


class DataHubClient:
    """Minimal GraphQL client with bearer auth and retries on transient errors."""

    def __init__(self, base_url: str, *, token: str | None = None, session=None):
        if not base_url:
            raise ValueError("datahub_source needs a base_url")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session = session or requests.Session()

    def query(self, query: str, variables: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {"query": query, "variables": variables or {}}
        for attempt in range(_RETRIES):
            try:
                response = self._session.post(
                    f"{self.base_url}/api/graphql", json=payload, headers=headers, timeout=_TIMEOUT
                )
            except requests.RequestException as exc:
                if attempt == _RETRIES - 1:
                    raise DataHubAPIError(
                        f"DataHub is unreachable at {self.base_url}: {self._redact(str(exc))}"
                    ) from None
            else:
                if response.status_code not in (429, 500, 502, 503, 504) or attempt == _RETRIES - 1:
                    break
            time.sleep(2**attempt)
        if response.status_code in (401, 403):
            raise DataHubAuthError(
                f"DataHub refused the request (HTTP {response.status_code}); check the token"
            )
        if response.status_code >= 400:
            raise DataHubAPIError(
                f"DataHub GraphQL failed with HTTP {response.status_code}: "
                f"{self._redact(response.text[:200])}"
            )
        body = response.json()
        # GraphQL reports failures in "errors", often next to partial data. A
        # partial page must not count as the snapshot, so any error aborts.
        if body.get("errors"):
            messages = "; ".join(str(e.get("message")) for e in body["errors"])[:300]
            if "Unauthorized" in messages or "401" in messages:
                raise DataHubAuthError(f"DataHub rejected the token: {messages}")
            raise DataHubAPIError(f"DataHub GraphQL returned errors: {self._redact(messages)}")
        if not isinstance(body.get("data"), dict):
            raise DataHubAPIError("DataHub GraphQL returned no data")
        return body["data"]

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _filters(platforms, domains, containers) -> list[dict]:
    clauses = []
    if platforms:
        values = [p if p.startswith("urn:li:") else f"urn:li:dataPlatform:{p}" for p in platforms]
        clauses.append({"field": "platform", "values": values})
    if domains:
        clauses.append({"field": "domains", "values": list(domains)})
    if containers:
        clauses.append({"field": "container", "values": list(containers)})
    return [{"and": clauses}] if clauses else []


def iter_datasets(
    client: DataHubClient,
    *,
    platforms=None,
    domains=None,
    containers=None,
    query: str = "*",
    check_active=None,
) -> Iterator[dict]:
    """Yield every dataset in scope, following ``scrollId`` until the last page."""
    scroll_id = None
    while True:
        if check_active is not None:
            check_active()
        search_input: dict[str, Any] = {"types": ["DATASET"], "query": query, "count": PAGE_SIZE}
        or_filters = _filters(platforms, domains, containers)
        if or_filters:
            search_input["orFilters"] = or_filters
        if scroll_id:
            search_input["scrollId"] = scroll_id
        page = client.query(SCROLL_QUERY, {"input": search_input})["scrollAcrossEntities"]
        for result in page.get("searchResults") or []:
            entity = result.get("entity") or {}
            if entity.get("urn"):
                yield entity
        scroll_id = page.get("nextScrollId")
        if not scroll_id or not page.get("searchResults"):
            return


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _clean(text: Any) -> str:
    return " ".join(str(text).split()) if text else ""


def _names(items: list[dict] | None, key: str) -> list[str]:
    names = []
    for item in items or []:
        ref = item.get(key) or {}
        name = (ref.get("properties") or {}).get("name") or ref.get("urn", "").rsplit(":", 1)[-1]
        if name:
            names.append(name)
    return sorted(set(names))


def _owner_label(owner: dict) -> str:
    entity = owner.get("owner") or {}
    props = entity.get("properties") or {}
    return (
        props.get("displayName")
        or entity.get("name")
        or entity.get("username")
        or entity.get("urn", "unknown")
    )


def _owner_type(owner: dict) -> str:
    custom = ((owner.get("ownershipType") or {}).get("info") or {}).get("name")
    return custom or _OWNERSHIP_TYPES.get(owner.get("type") or "NONE", "Owner")


def urn_name(urn: str) -> str | None:
    """The name part of a dataset URN, e.g. ``db.schema.table``, else ``None``."""
    if not urn.startswith("urn:li:dataset:(") or not urn.endswith(")"):
        return None
    parts = urn[len("urn:li:dataset:(") : -1].rsplit(",", 1)[0].split(",", 1)
    return parts[1] if len(parts) == 2 else None


def dataset_name(entity: dict) -> str:
    # The URN's name is the stable, fully qualified one (``db.schema.table``);
    # ``name`` is often just the table. It is also what other systems (an
    # Airflow asset URI, a dbt model) resolve the same table to.
    props = entity.get("properties") or {}
    return (
        urn_name(entity["urn"]) or props.get("qualifiedName") or entity.get("name") or entity["urn"]
    )


def render_dataset(entity: dict) -> str:
    name = dataset_name(entity)
    platform = (entity.get("platform") or {}).get("name") or "unknown"
    description = _clean((entity.get("editableProperties") or {}).get("description")) or _clean(
        (entity.get("properties") or {}).get("description")
    )
    containers = [
        c["properties"]["name"]
        for c in reversed((entity.get("parentContainers") or {}).get("containers") or [])
        if (c.get("properties") or {}).get("name")
    ]
    lines = [
        f"DataHub dataset {name}",
        f"URN: {entity['urn']}",
        f"Platform: {platform}; environment: {entity.get('origin') or 'unknown'}"
        + (f"; container: {' > '.join(containers)}" if containers else ""),
        f"Description: {description or 'none recorded in DataHub'}",
    ]
    for link in sorted(
        (entity.get("institutionalMemory") or {}).get("elements") or [],
        key=lambda e: e.get("url") or "",
    ):
        lines.append(f"Documentation link: {link.get('label') or 'link'} {link.get('url')}")

    owners = sorted(
        {
            (_owner_label(o), _owner_type(o))
            for o in (entity.get("ownership") or {}).get("owners") or []
        }
    )
    if owners:
        for label, kind in owners:
            # The ownership type gets its own sentence so the owner's name stays one entity.
            lines.append(f"Dataset {name} is owned by {label}.")
            if kind != "Owner":
                lines.append(f"{label} is the {kind} of dataset {name}.")
    else:
        lines.append(f"Dataset {name} has no owner recorded in DataHub.")
    domain = ((entity.get("domain") or {}).get("domain") or {}).get("properties") or {}
    lines.append(
        f"Dataset {name} belongs to domain {domain['name']}."
        if domain.get("name")
        else f"Dataset {name} has no domain recorded in DataHub."
    )
    for tag in _names((entity.get("tags") or {}).get("tags"), "tag"):
        lines.append(f"Dataset {name} is tagged {tag}.")
    for term in _names((entity.get("glossaryTerms") or {}).get("terms"), "term"):
        lines.append(f"Dataset {name} is linked to glossary term {term}.")

    upstream = entity.get("upstream") or {}
    relationships = upstream.get("relationships") or []
    # Upstream lineage can also return data jobs; only datasets are written as parents.
    parents = sorted(
        {
            (
                urn_name(rel["entity"]["urn"]) or rel["entity"].get("name") or rel["entity"]["urn"],
                (rel["entity"].get("platform") or {}).get("name"),
            )
            for rel in relationships
            if (rel.get("entity", {}).get("urn") or "").startswith("urn:li:dataset:")
        }
    )
    for parent, parent_platform in parents:
        where = f" ({parent_platform})" if parent_platform and parent_platform != platform else ""
        lines.append(
            f"Dataset {parent}{where} is upstream of {name}; {name} is derived from {parent}."
        )
    if not parents:
        lines.append(f"Dataset {name} has no upstream lineage recorded in DataHub.")
    if (upstream.get("total") or 0) > len(relationships):
        lines.append(
            f"(Upstream lineage was truncated: DataHub reports {upstream['total']} upstream "
            f"entities and only the first {len(relationships)} were read.)"
        )

    lines += ["", f"Columns of dataset {name}:"]
    lines += _render_columns(entity, name)
    return "\n".join(lines)


def _render_columns(entity: dict, name: str) -> list[str]:
    edits = {
        e["fieldPath"]: e
        for e in (entity.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or []
    }
    fields = (entity.get("schemaMetadata") or {}).get("fields") or []
    if not fields:
        return ["No schema recorded in DataHub."]
    lines = []
    for field in sorted(fields, key=lambda f: f["fieldPath"]):
        path = field["fieldPath"]
        edit = edits.get(path, {})
        description = _clean(edit.get("description")) or _clean(field.get("description"))
        line = f"- column {path} ({field.get('nativeDataType') or 'unknown type'})"
        lines.append(line + (f": {description}" if description else ""))
        tags = set(_names((field.get("globalTags") or {}).get("tags"), "tag"))
        tags |= set(_names((edit.get("globalTags") or {}).get("tags"), "tag"))
        for tag in sorted(tags):
            lines.append(f"  Column {path} of dataset {name} is tagged {tag}.")
        terms = set(_names((field.get("glossaryTerms") or {}).get("terms"), "term"))
        terms |= set(_names((edit.get("glossaryTerms") or {}).get("terms"), "term"))
        for term in sorted(terms):
            lines.append(f"  Column {path} of dataset {name} is linked to glossary term {term}.")
    return lines


def dataset_row(entity: dict, base_url: str) -> dict:
    return {
        "id": entity["urn"],
        "title": f"DataHub dataset {dataset_name(entity)}",
        "content": render_dataset(entity),
        "url": f"{base_url}/dataset/{quote(entity['urn'], safe='')}",
    }


def iter_rows(
    client: DataHubClient,
    *,
    platforms=None,
    domains=None,
    containers=None,
    query: str = "*",
    frontend_url: str | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield one row per in-scope, not soft-deleted dataset.

    The whole scope is read before the first row is yielded, so a failure on any
    page raises before dlt loads anything and staging keeps the last snapshot.
    """
    entities = list(
        iter_datasets(
            client,
            platforms=platforms,
            domains=domains,
            containers=containers,
            query=query,
            check_active=check_active,
        )
    )
    rows, removed = [], 0
    for entity in sorted(entities, key=lambda e: e["urn"]):
        if (entity.get("status") or {}).get("removed"):
            removed += 1
            continue
        rows.append(dataset_row(entity, (frontend_url or client.base_url).rstrip("/")))
    logger.info("DataHub: synced %d dataset(s), skipped %d soft-deleted.", len(rows), removed)
    yield from rows


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def datahub_source(
    *,
    base_url: str,
    token: str | None = None,
    platforms: list[str] | None = None,
    domains: list[str] | None = None,
    containers: list[str] | None = None,
    query: str = "*",
    frontend_url: str | None = None,
    resource_name: str = DATAHUB_TABLE_NAME,
    session: requests.Session | None = None,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource that yields DataHub dataset documents for ``cognee.remember``.

    Args:
        base_url: DataHub GMS, e.g. ``http://localhost:8080`` (DataHub Cloud:
            ``https://<tenant>.acryl.io/gms``).
        token: Personal access token, sent as ``Authorization: Bearer <token>``.
        platforms: Only sync datasets on these platforms (``"postgres"`` or a platform URN).
        domains: Only sync datasets in these domains (domain URNs).
        containers: Only sync datasets directly in these containers (container URNs).
        query: Search text to narrow the scope further (default ``*``).
        frontend_url: DataHub UI base for the links stored with each document.
        resource_name: Staging-table name; keep it fixed per DataHub instance and
            scope, since it scopes forget-on-delete.
        session: Pre-built ``requests.Session``, mainly a test injection point.
        check_active: Optional host authorization checkpoint.

    Returns:
        A ``dlt`` resource with ``primary_key="id"`` and ``write_disposition="replace"``.
        Pass ``max_rows_per_table=0`` to ``remember`` so a capped read can never look
        like deleted datasets.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("The DataHub connector requires cognee>=1.6.2 (table-scoped cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("resource_name must be a non-empty string")

    client = DataHubClient(base_url, token=token, session=session)

    @dlt.resource(name=resource_name, primary_key="id", write_disposition="replace")
    def datahub_datasets():
        rows = iter_rows(
            client,
            platforms=platforms,
            domains=domains,
            containers=containers,
            query=query,
            frontend_url=frontend_url,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = datahub_datasets()
    setattr(resource, DOCUMENT_SOURCE_ATTR, DATAHUB_SOURCE_TAG)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return resource
