"""Sanity connector for cognee: a ``dlt`` resource that turns Sanity content into memory.

Sync the published documents of chosen types from one Sanity dataset into cognee,
incrementally and with forget-on-delete.  The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_sanity import sanity_source

    await cognee.remember(
        sanity_source(project_id="abc123", dataset="production", document_types=["article"]),
        dataset_name="my_content",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by document id
        max_rows_per_table=0,        # 0 = no row cap (a dataset easily exceeds the default 50)
    )

Design
------
* **Auth**: an API token (a read-only Viewer token is enough), sent as a bearer
  token to the uncached ``api.sanity.io`` query API, never the CDN, so a sync
  always sees the latest writes.  Public datasets also work without a token.
* **Published content only**: every content query uses the ``published``
  perspective, so drafts (``drafts.*``) and release versions never reach cognee.
  Unpublishing a document removes it from that view, so it is forgotten until it
  is published again.
* **What changed**: each run lists ``_id`` and ``_rev`` of every selected
  document (small rows, paged by ``_id``) and fetches the full content only for
  new or changed revisions.  ``_rev`` changes on every write, so this is exact.
  ``_updatedAt`` only has second precision, and documents written in the same
  transaction share it, so a timestamp cursor would need tie handling to not
  skip them; the listing is needed for forget-on-delete anyway.
* **References**: references are resolved to the referenced document's name or
  title, so cognee sees "Author: Alice Smith" instead of an id.  Renaming the
  author does not change the article's ``_rev`` or ``_updatedAt``, so the
  connector also tracks the revisions of every referenced document and re-renders
  the documents that point to one that changed or was deleted.
* **Forget-on-delete**: documents missing from the complete listing were deleted
  (or unpublished, or their type was dropped from the selection) and are emitted
  as ``_deleted`` tombstones, which cognee's ``orphan_cleanup`` removes.  Every
  request happens before the first row is yielded, so a failed or partial run
  yields nothing, keeps the previous state and never forgets anything.
* **Schema**: when the project has deployed its Studio schema, the deployed
  schema document (``_.schemas.default``) is ingested as one extra document that
  describes the selected types and their fields.  Projects without one simply
  skip it.
* **Document path**: each document becomes a text document (Portable Text
  flattened to paragraphs) that flows through normal cognify, tagged with a node
  set per document type.

Limitations
-----------
* Images and files are not ingested; their alt text and captions are.
* References are resolved one level deep (the referenced document's name or
  title), not followed further.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("sanity_connector")

SOURCE_TAG = "sanity"
DEFAULT_API_VERSION = "v2025-02-19"
DEFAULT_DATASET = "production"
SCHEMA_DOCUMENT_ID = "_.schemas.default"
LIST_PAGE_SIZE = 1000
FETCH_CHUNK = 100
MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 30.0
# Fields tried, in order, for a document's title and for a reference's label.
TITLE_FIELDS = ("title", "name", "heading", "label", "headline")
_PROJECT_RE = re.compile(r"^[a-z0-9]+$")
_DATASET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_API_VERSION_RE = re.compile(r"^v(\d{4}-\d{2}-\d{2}|1|X)$")
# dlt snake-cases table names, so "My-Site" and "my_site" would share a table while
# keeping separate sync states. Only names dlt leaves unchanged are accepted.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class SanityError(RuntimeError):
    """A Sanity API request failed (after retries, for transient errors)."""


class SanityAuthError(SanityError):
    """The token was rejected (401) or lacks read access to the dataset (403)."""


class SanityRateLimitError(SanityError):
    """Sanity kept answering 429 after the retries."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class SanityClient:
    """Minimal client for Sanity's GROQ query API.

    ``check_active`` (from a host) runs before every request, so a sync whose
    access was revoked stops at the next request rather than at the next row.
    """

    def __init__(
        self,
        project_id: str,
        dataset: str,
        token: str | None = None,
        *,
        api_version: str = DEFAULT_API_VERSION,
        session: Any = None,
        check_active: Callable[[], None] | None = None,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.project_id = project_id
        self.dataset = dataset
        self.url = f"https://{project_id}.api.sanity.io/{api_version}/data/query/{dataset}"
        self._token = token
        self._session = session
        self._check_active = check_active
        self._timeout = timeout
        self._sleep = sleep

    def __repr__(self) -> str:
        # Never show the token, whatever ends up printing or logging the client.
        return f"SanityClient(project_id={self.project_id!r}, dataset={self.dataset!r})"

    def _http(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def query(self, groq: str, params: dict | None = None, perspective: str = "published"):
        """Run one GROQ query and return its ``result``.

        POST keeps long id lists out of the URL.  429, 5xx and connection errors
        are retried with backoff (``Retry-After`` when given); anything else
        raises at once.
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        body = {"query": groq, "params": params or {}}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._check_active is not None:
                self._check_active()
            try:
                response = self._http().post(
                    self.url,
                    params={"perspective": perspective},
                    json=body,
                    headers=headers,
                    timeout=self._timeout,
                    allow_redirects=False,  # the token only ever goes to the project's API host
                )
            except OSError as exc:  # requests' ConnectionError and Timeout are OSErrors
                if attempt == MAX_ATTEMPTS:
                    raise SanityError(f"Sanity query failed: {type(exc).__name__}") from exc
                self._sleep(_backoff(attempt, None))
                continue
            status = response.status_code
            if status == 200:
                try:
                    return response.json().get("result")
                except (ValueError, AttributeError):
                    # ``from None``: the decode error keeps the whole body (``.doc``).
                    raise SanityError("Sanity answered HTTP 200 without a JSON result.") from None
            if (status == 429 or status >= 500) and attempt < MAX_ATTEMPTS:
                self._sleep(_backoff(attempt, response.headers.get("Retry-After")))
                continue
            detail = _error_detail(response)
            if status == 429:
                raise SanityRateLimitError("Sanity kept answering HTTP 429 (rate limited).")
            if status in (401, 403):
                raise SanityAuthError(
                    f"Sanity rejected the token for project '{self.project_id}', dataset "
                    f"'{self.dataset}' (HTTP {status}{': ' + detail if detail else ''}). "
                    "Create a token with Viewer access in sanity.io/manage -> API -> Tokens."
                )
            raise SanityError(
                f"Sanity query failed with HTTP {status}{': ' + detail if detail else ''}"
            )
        raise SanityError("Sanity query failed")  # pragma: no cover - loop always returns/raises

    # -- the queries the sync needs -----------------------------------------
    def list_revisions(self, scope: _Scope) -> dict[str, dict]:
        """``_id -> {rev, type}`` for every selected published document, paged by ``_id``.

        Reads until an empty page, so a short page is never taken as the end.
        """
        groq = (
            f"*[{scope.filter} && _id > $after] | order(_id asc) "
            f"[0...{LIST_PAGE_SIZE}]{{_id, _rev, _type}}"
        )
        out: dict[str, dict] = {}
        after = ""
        while True:
            page = self.query(groq, {**scope.params, "after": after}) or []
            if not page:
                return out
            if all(doc["_id"] in out for doc in page):
                # A page of ids already seen would repeat forever.
                raise SanityError("Sanity's id listing did not move forward; stopping.")
            for doc in page:
                out[doc["_id"]] = {"rev": doc["_rev"], "type": doc["_type"]}
            after = page[-1]["_id"]

    def get_documents(self, ids: Iterable[str]) -> list[dict]:
        """Full published documents by id."""
        docs: list[dict] = []
        for chunk in _chunks(sorted(ids), FETCH_CHUNK):
            docs.extend(self.query("*[_id in $ids]", {"ids": chunk}) or [])
        return docs

    def get_labels(self, ids: Iterable[str]) -> dict[str, dict]:
        """``_id -> {rev, type, label}`` for referenced documents (published)."""
        fields = ", ".join(TITLE_FIELDS)
        groq = f'*[_id in $ids]{{_id, _rev, _type, {fields}, "slug": slug.current}}'
        out: dict[str, dict] = {}
        for chunk in _chunks(sorted(ids), FETCH_CHUNK):
            for doc in self.query(groq, {"ids": chunk}) or []:
                out[doc["_id"]] = {
                    "rev": doc.get("_rev"),
                    "type": doc.get("_type"),
                    "label": _first_text(doc, (*TITLE_FIELDS, "slug")) or doc["_id"],
                }
        return out

    def get_schema(self) -> dict | None:
        """The deployed Studio schema document, or None if the project has none.

        It is a system document, so it is read with the ``raw`` perspective.
        """
        result = self.query("*[_id == $id][0]", {"id": SCHEMA_DOCUMENT_ID}, perspective="raw")
        return result or None


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            seconds = float(retry_after)
            if 0 <= seconds < float("inf"):
                return min(seconds, MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    return min(2.0 ** (attempt - 1), MAX_BACKOFF_SECONDS)


def _error_detail(response) -> str:
    """Sanity's own error description, without echoing request data."""
    try:
        error = (response.json() or {}).get("error")
    except ValueError:
        return ""
    if isinstance(error, dict):
        return str(error.get("description") or error.get("type") or "")[:200]
    return str(error or "")[:200]


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------------------
# Sync state machine (pure given a client + state dict, so unit-testable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Scope:
    project_id: str
    dataset: str
    document_types: tuple[str, ...]
    groq_filter: str | None = None
    include_schema: bool = True
    allow_empty_listing: bool = False

    @property
    def key(self) -> str:
        return f"{self.project_id}:{self.dataset}"

    @property
    def filter(self) -> str:
        # System documents (``_.*``) are never content, whatever their type.
        expr = '_type in $types && !(_id in path("_.**"))'
        if self.groq_filter:
            expr += f" && ({self.groq_filter})"
        return expr

    @property
    def params(self) -> dict:
        return {"types": list(self.document_types)}


def _iter_rows(client: SanityClient, scope: _Scope, state: dict, stats: dict | None = None):
    """Yield one row per new or changed document, plus hard-delete tombstones.

    ``state`` is a plain dict standing in for dlt's resource state.  Every API
    call happens before the first yield, and the state is only written after the
    last one, so a failed run changes nothing.  A rejected token raises; a rate
    limit that outlasts the retries ends the run without rows or a state change
    and is reported in ``stats``, so the next sync simply starts again.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(listed=0, changed=0, reference_updates=0, deleted=0, schema=0, failed=0)
    try:
        plan = _plan(client, scope, state, stats)
    except SanityAuthError:
        stats.update(failed=1, failed_auth=1)
        raise
    except SanityRateLimitError:
        stats.update(failed=1, failed_rate_limit=1)
        logger.warning("Sanity: rate limited; stopping this run, the next sync resumes.")
        return
    except SanityError:
        stats.update(failed=1)
        raise
    yield from _emit(scope, state, stats, plan)


@dataclass
class _Plan:
    """The outcome of a run's requests, before any row is emitted."""

    live: dict[str, dict]
    unchanged: set[str]
    fetched: dict[str, dict]
    refs: dict[str, list[str]]
    labels: dict[str, dict]
    schema: dict | None
    gone: list[str]


def _plan(client: SanityClient, scope: _Scope, state: dict, stats: dict) -> _Plan:
    """Make every request of a run: what to delete, what to render, with which labels."""
    if state.get("source") not in (None, scope.key):
        raise ValueError(
            f"This sync state belongs to Sanity '{state['source']}', not '{scope.key}'. "
            "Give each project and dataset its own resource_name."
        )
    old_revs: dict[str, str] = dict(state.get("revs", {}))
    old_refs: dict[str, list[str]] = {k: list(v) for k, v in state.get("refs", {}).items()}
    old_ref_revs: dict[str, str] = dict(state.get("ref_revs", {}))

    # 1. What exists now, with the revision of each document.
    live = client.list_revisions(scope)
    stats["listed"] = len(live)
    if not live and old_revs and not scope.allow_empty_listing:
        # A private dataset read without a working token answers with no documents
        # instead of an error, and so does a mistyped document type. Either would
        # forget everything, so an empty listing never deletes on its own.
        raise SanityError(
            f"Sanity listed no documents of the selected types, but {len(old_revs)} were "
            "synced before, so nothing is forgotten. Check the token, document_types and "
            "groq_filter. If they really were all deleted, run once with "
            "allow_empty_listing=True."
        )
    to_fetch = {doc_id for doc_id, info in live.items() if old_revs.get(doc_id) != info["rev"]}
    stats["changed"] = len(to_fetch)

    # 2. Documents whose content did not change but whose referenced documents
    #    were renamed or deleted: their rendered text is stale.
    unchanged = set(live) - to_fetch
    watched = {ref for doc_id in unchanged for ref in old_refs.get(doc_id, [])}
    labels = client.get_labels(watched) if watched else {}
    moved = {ref for ref in watched if labels.get(ref, {}).get("rev") != old_ref_revs.get(ref)}
    stale = {doc_id for doc_id in unchanged if moved & set(old_refs.get(doc_id, []))}
    stats["reference_updates"] = len(stale)
    to_fetch |= stale

    # 3. Full content for everything to (re-)render, and labels for new references.
    docs = client.get_documents(to_fetch) if to_fetch else []
    fetched = {doc["_id"]: doc for doc in docs}
    new_refs = {doc_id: sorted(_references(doc)) for doc_id, doc in fetched.items()}
    missing_labels = {ref for refs in new_refs.values() for ref in refs} - set(labels)
    if missing_labels:
        labels.update(client.get_labels(missing_labels))

    # 4. The schema document, when one is deployed and selected.
    schema = client.get_schema() if scope.include_schema else None

    # A document listed but gone by the time it was fetched (deleted mid-run)
    # is treated as deleted; the next run confirms it.
    vanished = (to_fetch - set(fetched)) & set(old_revs)
    refs = {doc_id: old_refs.get(doc_id, []) for doc_id in unchanged}
    refs.update(new_refs)
    return _Plan(
        live=live,
        unchanged=unchanged,
        fetched=fetched,
        refs=refs,
        labels=labels,
        schema=schema,
        gone=sorted((set(old_revs) - set(live)) | vanished),
    )


def _emit(scope: _Scope, state: dict, stats: dict, plan: _Plan):
    """Yield the rows of a planned run, then write the state for the next one."""
    for doc_id in plan.gone:
        stats["deleted"] += 1
        yield {"id": _row_id(scope, doc_id), "_deleted": True}

    for doc_id in sorted(plan.fetched):
        yield _document_row(scope, plan.fetched[doc_id], plan.labels)

    # The schema row is compared by its rendered text, which also covers a
    # change of the selected types.
    schema_row = _schema_row(scope, plan.schema) if plan.schema else None
    schema_hash = _hash(schema_row["content"]) if schema_row else None
    if scope.include_schema and plan.schema is None and state.get("schema_hash"):
        # The schema is a system document that only a token can read, even in a
        # public dataset, so "no schema" can just mean the token stopped working.
        # Keep what was synced; turning include_schema off is what forgets it.
        logger.warning("Sanity: the deployed schema could not be read; keeping the synced one.")
        schema_hash = state["schema_hash"]
    elif schema_hash != state.get("schema_hash"):
        if schema_row:
            stats["schema"] = 1
            yield schema_row
        else:
            yield {"id": _schema_row_id(scope), "_deleted": True}

    # State for the next run, written only once every row is out.
    kept = plan.unchanged | set(plan.fetched)
    tracked = {ref for doc_refs in plan.refs.values() for ref in doc_refs}
    state["source"] = scope.key
    state["revs"] = {doc_id: plan.live[doc_id]["rev"] for doc_id in plan.live if doc_id in kept}
    state["refs"] = {doc_id: doc_refs for doc_id, doc_refs in plan.refs.items() if doc_refs}
    # A reference whose target is missing is stored without a revision, so it
    # only counts as moved again once the target comes back.
    state["ref_revs"] = {ref: plan.labels[ref]["rev"] for ref in tracked if ref in plan.labels}
    state["schema_hash"] = schema_hash
    logger.info("Sanity: sync stats %s", stats)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_id(scope: _Scope, doc_id: str) -> str:
    return f"{SOURCE_TAG}:{scope.key}:{doc_id}"


def _schema_row_id(scope: _Scope) -> str:
    return f"{SOURCE_TAG}:{scope.key}:{SCHEMA_DOCUMENT_ID}"


def _references(value: Any) -> set[str]:
    """Every document id a value references (Portable Text links included)."""
    found: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("_ref")
        if isinstance(ref, str) and not _is_asset(ref):
            found.add(ref.removeprefix("drafts."))
        for key, item in value.items():
            if key != "_ref":
                found |= _references(item)
    elif isinstance(value, list):
        for item in value:
            found |= _references(item)
    return found


def _is_asset(ref: str) -> bool:
    return ref.startswith(("image-", "file-"))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _document_row(scope: _Scope, doc: dict, labels: dict[str, dict]) -> dict:
    title_field = next((f for f in TITLE_FIELDS if _plain(doc.get(f))), None)
    title = _plain(doc.get(title_field)) if title_field else f"{doc['_type']} {doc['_id']}"
    lines = [f"Sanity {doc['_type']} in {scope.dataset}"]
    paragraphs: list[str] = []
    # Sorted, so the same document always renders the same text whatever order
    # Sanity returns its fields in (a reordering must not re-cognify it).
    for key, value in sorted(doc.items()):
        if key.startswith("_") or key == title_field:
            continue
        rendered = _render(value, labels)
        if not rendered:
            continue
        if "\n" in rendered or len(rendered) > 200:
            paragraphs.append(f"{_label(key)}:\n{rendered}")
        else:
            lines.append(f"{_label(key)}: {rendered}")
    content = "\n".join(lines) + ("\n\n" + "\n\n".join(paragraphs) if paragraphs else "")
    return {
        "id": _row_id(scope, doc["_id"]),
        "title": title,
        "content": content,
        "url": None,
        dlt_utils.NODE_SET_COLUMN: [doc["_type"]],
        "_deleted": False,
    }


def _render(value: Any, labels: dict[str, dict], depth: int = 0) -> str:
    """Readable text for one field value, or "" when there is nothing to say."""
    if value is None or depth > 4:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        if value and all(_is_block(v) for v in value):
            return _portable_text(value, labels)
        if any(_is_block(v) for v in value):
            # Portable Text with custom blocks (images, code) between paragraphs.
            parts = (
                _portable_text([v], labels) if _is_block(v) else _render(v, labels, depth + 1)
                for v in value
            )
            return "\n".join(p for p in parts if p)
        parts = [_render(v, labels, depth + 1) for v in value]
        parts = [p for p in parts if p]
        if all("\n" not in p and len(p) < 80 for p in parts):
            return ", ".join(parts)
        return "\n".join(f"- {p}" for p in parts)
    if isinstance(value, dict):
        kind = value.get("_type")
        if "_ref" in value:
            if _is_asset(value["_ref"]):
                return ""
            ref = value["_ref"].removeprefix("drafts.")
            return labels.get(ref, {}).get("label") or "(missing reference)"
        if kind == "slug":
            return _plain(value.get("current"))
        if kind in ("image", "file") or "asset" in value:
            return "; ".join(
                _plain(value[k]) for k in ("alt", "caption", "title") if _plain(value.get(k))
            )
        if kind == "block":
            return _portable_text([value], labels)
        if kind == "code" and isinstance(value.get("code"), str):
            return value["code"].strip()
        parts = []
        for key, item in sorted(value.items()):
            if key.startswith("_"):
                continue
            rendered = _render(item, labels, depth + 1)
            if rendered:
                parts.append(f"{_label(key)}: {rendered}")
        return "; ".join(parts) if all("\n" not in p for p in parts) else "\n".join(parts)
    return ""


def _is_block(value: Any) -> bool:
    return isinstance(value, dict) and value.get("_type") == "block"


def _portable_text(blocks: list[dict], labels: dict[str, dict]) -> str:
    """Portable Text to plain paragraphs; list items get a dash, links keep their target."""
    out: list[str] = []
    for block in blocks:
        mark_defs = {m.get("_key"): m for m in block.get("markDefs") or [] if isinstance(m, dict)}
        text = ""
        for child in block.get("children") or []:
            if not isinstance(child, dict):
                continue
            span = child.get("text") or ""
            for mark in child.get("marks") or []:
                definition = mark_defs.get(mark)
                reference = (definition or {}).get("reference")
                if isinstance(reference, dict) and "_ref" in reference:
                    target = _render(reference, labels)
                    span += f" ({target})" if target else ""
                elif definition and definition.get("href"):
                    span += f" ({definition['href']})"
            text += span
        text = text.strip()
        if not text:
            continue
        if block.get("listItem"):
            text = "  " * max(int(block.get("level") or 1) - 1, 0) + f"- {text}"
        out.append(text)
    return "\n".join(out)


def _plain(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_text(doc: dict, fields: Iterable[str]) -> str:
    return next((_plain(doc.get(f)) for f in fields if _plain(doc.get(f))), "")


def _label(key: str) -> str:
    """``publishedAt`` -> ``Published at``; ``seo_title`` -> ``Seo title``."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key).replace("_", " ").split()
    return " ".join(words).capitalize() if words else key


def _schema_row(scope: _Scope, schema: dict) -> dict | None:
    """One document describing the selected types, from the deployed schema."""
    types = {t["name"]: t for t in _schema_types(schema) if t.get("name")}
    selected = [types[name] for name in scope.document_types if name in types]
    if not selected:
        return None
    lines = [f"The content model of the Sanity dataset {scope.dataset}."]
    for type_def in selected:
        title = type_def.get("title")
        lines.append("")
        lines.append(
            f"Document type {type_def['name']}"
            + (f" ({title})" if title and title != type_def["name"] else "")
        )
        for field in type_def.get("fields") or []:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            lines.append(f"- {field['name']}: {_field_kind(field)}")
    return {
        "id": _schema_row_id(scope),
        "title": f"Sanity content model: {scope.dataset}",
        "content": "\n".join(lines),
        "url": None,
        dlt_utils.NODE_SET_COLUMN: ["schema"],
        "_deleted": False,
    }


def _schema_types(schema: dict) -> list[dict]:
    """The type definitions in a deployed schema document.

    The definitions may sit under any key, and may be stored as a JSON string, so
    look for the first list of objects that have a ``name`` and ``fields``.
    """
    stack: list[Any] = [schema]
    while stack:
        value = stack.pop(0)
        if isinstance(value, str) and value[:1] in "[{":
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, list):
            if any(isinstance(v, dict) and "name" in v and "fields" in v for v in value):
                return [v for v in value if isinstance(v, dict)]
            stack.extend(value)
        elif isinstance(value, dict):
            stack.extend(v for k, v in value.items() if not k.startswith("_"))
    return []


def _field_kind(field: dict) -> str:
    kind = field.get("type") or "unknown"
    if kind == "reference":
        targets = [t.get("type") for t in field.get("to") or [] if isinstance(t, dict)]
        return "reference to " + (" or ".join(filter(None, targets)) or "a document")
    if kind == "array":
        members = field.get("of") or []
        names = [m.get("type") for m in members if isinstance(m, dict)]
        if "block" in names:
            return "rich text (Portable Text)"
        if "reference" in names:
            targets = [
                t.get("type")
                for m in members
                if isinstance(m, dict)
                for t in m.get("to") or []
                if isinstance(t, dict)
            ]
            return "list of references to " + (" or ".join(filter(None, targets)) or "documents")
        return "list of " + (", ".join(filter(None, names)) or "items")
    return str(kind)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def sanity_source(
    project_id: str | None = None,
    dataset: str | None = None,
    document_types: Iterable[str] | str | None = None,
    *,
    token: str | None = None,
    api_version: str = DEFAULT_API_VERSION,
    groq_filter: str | None = None,
    include_schema: bool = True,
    allow_empty_listing: bool = False,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Return a ``dlt`` resource yielding one row per new or changed Sanity document.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        project_id: The Sanity project id, like ``"abc123xy"``.
        dataset: The dataset name (default ``production``).
        document_types: Document types to sync, like ``["article", "author"]``.
            Required: a dataset also holds system and plugin documents that are
            not content.
        token: API token; a Viewer token is enough.  Public datasets can be read
            without one.  It is only taken as an argument (the example reads it
            from the environment).
        api_version: Sanity API version date, pinned so responses stay stable.
        groq_filter: Optional extra GROQ condition, like ``'language == "en"'``.
        include_schema: Also ingest the deployed Studio schema for the selected
            types, when the project has one.
        allow_empty_listing: Let a run whose listing comes back empty forget
            everything synced before.  Off by default, because a private dataset
            read without a working token, or a mistyped type, also lists nothing.
        resource_name: Stable dlt resource name.  Defaults to
            ``sanity_<project>_<dataset>``, so two projects or datasets synced into
            one cognee dataset keep separate tables and state.
        check_active: Optional host authorization checkpoint, run before every
            request and every row.
        client: A pre-built client, mainly for tests.  ``service`` is an alias.

    After a run, ``resource.cognee_sync_stats`` holds counts only: ``listed``,
    ``changed``, ``reference_updates``, ``deleted``, ``schema`` and ``failed``,
    plus ``failed_auth`` or ``failed_rate_limit`` when a run stopped for that.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "Sanity sync requires cognee 1.6.3 or later (table-scoped document cleanup and "
            "per-row node sets). Upgrade cognee before syncing."
        )

    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    client = client if client is not None else service
    project_id = (project_id or "").strip()
    dataset = (dataset or DEFAULT_DATASET).strip()
    if not _PROJECT_RE.match(project_id):
        raise ValueError("Pass project_id=, the Sanity project id, e.g. 'abc123xy'.")
    if not _DATASET_RE.match(dataset):
        raise ValueError(f"'{dataset}' is not a valid Sanity dataset name.")
    if not _API_VERSION_RE.match(api_version):
        raise ValueError(f"'{api_version}' is not a Sanity API version like 'v2025-02-19'.")
    scope = _Scope(
        project_id=project_id,
        dataset=dataset,
        document_types=_normalize_types(document_types),
        groq_filter=(groq_filter or "").strip() or None,
        include_schema=include_schema,
        allow_empty_listing=allow_empty_listing,
    )
    if client is None:
        client = SanityClient(
            project_id,
            dataset,
            (token or "").strip() or None,
            api_version=api_version,
            check_active=check_active,
        )
    if resource_name is not None and not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, starting with a "
            "letter, so dlt uses it unchanged as the table name."
        )
    resource_name = resource_name or re.sub(r"[^a-z0-9_]", "_", f"sanity_{project_id}_{dataset}")

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        columns={
            # `_deleted` is a boolean hard-delete marker: rows where it is True are
            # removed from the dlt destination on merge, which propagates the
            # deletion through cognee's orphan_cleanup.
            "_deleted": {"data_type": "bool", "hard_delete": True},
            # Typed up front so a deletion-only run still has these columns.
            "title": {"data_type": "text", "nullable": True},
            "content": {"data_type": "text", "nullable": True},
            "url": {"data_type": "text", "nullable": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def sanity_documents():
        yield from dlt_utils.guarded_rows(
            _iter_rows(client, scope, dlt.current.resource_state(), stats),
            check_active,
        )

    resource = sanity_documents()
    # Opt into the document ingestion path: each Sanity document becomes a text
    # document that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never titles or content.
    resource.cognee_sync_stats = stats
    return resource


def _normalize_types(document_types: Iterable[str] | str | None) -> tuple[str, ...]:
    if document_types is None:
        document_types = ()
    elif isinstance(document_types, str):
        document_types = document_types.split(",")
    cleaned = tuple(dict.fromkeys(t.strip() for t in document_types if t and t.strip()))
    if not cleaned:
        # An empty selection would forget every synced document, so refuse it.
        raise ValueError(
            'document_types is empty. Pass the types to sync, like ["article", "author"].'
        )
    bad = [t for t in cleaned if not _TYPE_RE.match(t)]
    if bad:
        raise ValueError(f"Not valid Sanity document type names: {bad}")
    return cleaned
