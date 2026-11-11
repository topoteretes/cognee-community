"""Elasticsearch vector adapter for cognee.

One Elasticsearch index per cognee collection (``DocumentChunk_text``,
``Entity_name``, ...), named ``<prefix>-<collection>``. Datasets share these
indices: every document carries a ``database_name`` keyword that scopes
searches, deletes and listings, and the Elasticsearch ``_id`` is derived from
``(database_name, data point id)`` so two datasets can hold the same cognee
entity independently. Payloads keep cognee's original ids.

Vectors live in a ``dense_vector`` field indexed with HNSW and cosine
similarity. Elasticsearch reports kNN scores as ``(1 + cosine) / 2``; the
adapter converts them back to cosine distance so lower is better, as the
cognee vector contract requires.
"""

import asyncio
import os
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.databases.vector.exceptions import CollectionNotFoundError
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.shared.logging_utils import get_logger
from elasticsearch import AsyncElasticsearch, BadRequestError, NotFoundError

logger = get_logger("ElasticsearchAdapter")

DEFAULT_PORT = 9200
DEFAULT_INDEX_PREFIX = "cognee"
# index.max_result_window default and the ceiling for knn ``k``.
MAX_RESULT_WINDOW = 10_000
MIN_NUM_CANDIDATES = 100
SCORE_ID_BATCH_SIZE = 1_000

# Characters Elasticsearch rejects in index names; a name must also be lowercase
# and must not start with ``-``, ``_`` or ``+``.
_INVALID_INDEX_CHARS = re.compile(r'[\\/*?"<>|,#: ]')

# Painless script for remove_belongs_to_set_tags: strip the tags from the
# indexed keyword array and from the stored payload, then delete rows that end
# up with no tags at all.
REMOVE_TAGS_SCRIPT = """
if (ctx._source.belongs_to_set != null) {
  ctx._source.belongs_to_set.removeIf(tag -> params.tags.contains(tag));
}
if (ctx._source.payload != null && ctx._source.payload.belongs_to_set != null) {
  ctx._source.payload.belongs_to_set.removeIf(tag -> params.tags.contains(tag));
}
if (ctx._source.belongs_to_set == null || ctx._source.belongs_to_set.isEmpty()) {
  ctx.op = 'delete';
}
""".strip()


def _body(response: Any) -> Any:
    """Unwrap an elastic_transport ApiResponse into its plain body."""
    return getattr(response, "body", response)


def _env_flag(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


class IndexSchema(DataPoint):
    """Row shape cognee indexes per embedded field, mirroring cognee core's IndexSchema.

    Besides the text and its node-set tags it carries the reference scalars that
    retrieval reads back from search payloads (evidence, references, hybrid
    context). They stay None for data points that lack them.
    """

    text: str

    document_id: str | None = None
    document_name: str | None = None
    chunk_index: int | None = None
    source_chunk_id: str | None = None
    importance_weight: float | None = 0.5
    # The document's external_metadata as JSON text, copied onto chunks at ingest.
    external_metadata: str | None = None

    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: list[str] = []


class ElasticsearchAdapter(VectorDBInterface):
    name = "Elasticsearch"
    supports_payload_update = True

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        embedding_engine: EmbeddingEngine = None,
        database_name: str = "cognee_db",
        vector_db_host: str = "",
        vector_db_port: str = "",
        vector_db_username: str = "",
        vector_db_password: str = "",
        cloud_id: str | None = None,
        index_prefix: str | None = None,
        verify_certs: bool | None = None,
        ca_certs: str | None = None,
        request_timeout: int = 120,
        client: AsyncElasticsearch | None = None,
        **kwargs,
    ):
        """
        Parameters:
            url: Elasticsearch endpoint(s), each a full ``http(s)://`` URL;
                comma-separate several nodes. Required unless a cloud id is set,
                which takes precedence. Through cognee this is ``VECTOR_DB_URL``:
                cognee never passes an empty url (it substitutes its LanceDB path),
                so the ``http://<vector_db_host>:<vector_db_port>`` and
                ``http://localhost:9200`` fallbacks apply only when the adapter is
                constructed directly with no url.
            api_key: Elasticsearch API key, either the encoded form the API returns
                or ``"<id>:<api_key>"``. Takes precedence over basic auth.
            embedding_engine: cognee embedding engine used for all text.
            database_name: dataset scope; one adapter instance serves one dataset.
            vector_db_host / vector_db_port / vector_db_username / vector_db_password:
                the connection keywords cognee >= 1.5 passes to every adapter.
                Username and password become HTTP basic auth.
            cloud_id: Elastic Cloud deployment id (or ``ELASTICSEARCH_CLOUD_ID``).
            index_prefix: prefix of every index this adapter manages
                (or ``ELASTICSEARCH_INDEX_PREFIX``; default ``cognee``). ``prune``
                and collection listing only ever touch ``<prefix>-*``.
            verify_certs / ca_certs: TLS settings (or ``ELASTICSEARCH_VERIFY_CERTS``
                / ``ELASTICSEARCH_CA_CERTS``).
            request_timeout: per-request timeout in seconds.
            client: an existing ``AsyncElasticsearch`` to use instead of building one.
        """
        self.embedding_engine = embedding_engine
        self.database_name = database_name
        self.VECTOR_DB_LOCK = asyncio.Lock()

        self.cloud_id = cloud_id or os.getenv("ELASTICSEARCH_CLOUD_ID") or None
        if url:
            self.hosts = [host.strip() for host in url.split(",") if host.strip()]
        elif vector_db_host:
            self.hosts = [f"http://{vector_db_host}:{vector_db_port or DEFAULT_PORT}"]
        elif self.cloud_id:
            self.hosts = []
        else:
            self.hosts = [f"http://localhost:{DEFAULT_PORT}"]
        self.url = self.hosts[0] if self.hosts else None

        self.api_key = api_key or None
        self.basic_auth = (
            (vector_db_username, vector_db_password)
            if vector_db_username and vector_db_password
            else None
        )

        self.index_prefix = self._sanitize_prefix(
            index_prefix or os.getenv("ELASTICSEARCH_INDEX_PREFIX") or DEFAULT_INDEX_PREFIX
        )

        if verify_certs is None:
            verify_certs = _env_flag("ELASTICSEARCH_VERIFY_CERTS")
        self.verify_certs = verify_certs
        self.ca_certs = ca_certs or os.getenv("ELASTICSEARCH_CA_CERTS") or None
        self.request_timeout = request_timeout

        self.client = client if client is not None else self._build_client()

    # ------------------------------------------------------------------ setup

    def _build_client(self) -> AsyncElasticsearch:
        client_kwargs: dict[str, Any] = {"request_timeout": self.request_timeout}
        if self.cloud_id:
            client_kwargs["cloud_id"] = self.cloud_id
        else:
            client_kwargs["hosts"] = self.hosts
        if self.api_key:
            client_kwargs["api_key"] = (
                tuple(self.api_key.split(":", 1)) if ":" in self.api_key else self.api_key
            )
        elif self.basic_auth:
            client_kwargs["basic_auth"] = self.basic_auth
        if self.verify_certs is not None:
            client_kwargs["verify_certs"] = self.verify_certs
        if self.ca_certs:
            client_kwargs["ca_certs"] = self.ca_certs
        return AsyncElasticsearch(**client_kwargs)

    async def close(self) -> None:
        await self.client.close()

    @staticmethod
    def _sanitize_prefix(prefix: str) -> str:
        cleaned = _INVALID_INDEX_CHARS.sub("-", prefix.strip().lower()).lstrip("-_+")
        if not cleaned:
            raise ValueError(
                "ElasticsearchAdapter needs a non-empty index prefix; it bounds which "
                "indices prune() and get_collection_names() may touch."
            )
        return cleaned

    def _index(self, collection_name: str) -> str:
        """Elasticsearch index name for a cognee collection."""
        name = _INVALID_INDEX_CHARS.sub("-", collection_name.strip().lower())
        return f"{self.index_prefix}-{name}"

    def _doc_id(self, data_point_id: Any) -> str:
        """Scope the Elasticsearch ``_id`` to this dataset; payloads keep cognee's id."""
        namespace = uuid5(NAMESPACE_URL, self.database_name)
        return str(uuid5(namespace, str(data_point_id)))

    def _dataset_filter(self) -> dict:
        return {"term": {"database_name": self.database_name}}

    @staticmethod
    def _node_set_filters(node_name: list[str] | None, operator: str) -> list[dict]:
        if not node_name:
            return []
        names = [str(name) for name in node_name]
        if operator == "AND":
            return [{"term": {"belongs_to_set": name}} for name in names]
        return [{"terms": {"belongs_to_set": names}}]

    def _filters(self, node_name: list[str] | None = None, operator: str = "OR") -> list[dict]:
        return [self._dataset_filter(), *self._node_set_filters(node_name, operator)]

    def _document(self, data_point_id: Any, payload: dict, vector: list[float]) -> dict:
        return {
            "id": str(data_point_id),
            "database_name": self.database_name,
            "belongs_to_set": [str(tag) for tag in (payload.get("belongs_to_set") or [])],
            "payload": payload,
            "vector": vector,
        }

    @staticmethod
    def _source_fields(include_payload: bool) -> list[str]:
        return ["id", "payload"] if include_payload else ["id"]

    @staticmethod
    def _knn_distance(score: float) -> float:
        # Elasticsearch scores cosine kNN hits as (1 + cosine) / 2.
        return 2.0 * (1.0 - float(score))

    def _scored(self, hit: dict, include_payload: bool) -> ScoredResult:
        source = hit["_source"]
        return ScoredResult(
            id=parse_id(source["id"]),
            payload=source.get("payload") if include_payload else None,
            score=self._knn_distance(hit["_score"]),
        )

    def _knn(self, query_vector: list[float], k: int, filters: list[dict]) -> dict:
        return {
            "field": "vector",
            "query_vector": query_vector,
            "k": k,
            "num_candidates": min(max(k * 2, MIN_NUM_CANDIDATES), MAX_RESULT_WINDOW),
            "filter": {"bool": {"filter": filters}},
        }

    async def _resolve_limit(self, index: str, limit: int | None, filters: list[dict]) -> int:
        """``None`` means every matching row; ``<= 0`` means none. Capped at the window."""
        if limit is None:
            response = await self.client.count(index=index, query={"bool": {"filter": filters}})
            limit = _body(response)["count"]
            if limit > MAX_RESULT_WINDOW:
                logger.warning(
                    "Index %s holds %d matching rows; a search without a limit returns only "
                    "the nearest %d (Elasticsearch's result window).",
                    index,
                    limit,
                    MAX_RESULT_WINDOW,
                )
        if limit <= 0:
            return 0
        return min(limit, MAX_RESULT_WINDOW)

    async def _bulk(self, operations: list[dict], ignore: tuple[str, ...] = ()) -> None:
        if not operations:
            return
        response = _body(await self.client.bulk(operations=operations, refresh=True))
        if not response.get("errors"):
            return
        failures = []
        for item in response.get("items", []):
            action, result = next(iter(item.items()))
            error = result.get("error")
            if error and error.get("type") not in ignore:
                failures.append(
                    f"{action} {result.get('_id')}: {error.get('type')}: {error.get('reason')}"
                )
        if failures:
            raise RuntimeError(
                f"Elasticsearch bulk request failed for {len(failures)} document(s): "
                + "; ".join(failures[:5])
            )

    async def _stored_belongs_to_set(self, index: str, doc_ids: list[str]) -> dict[str, list]:
        """This dataset's non-empty stored ``belongs_to_set`` per Elasticsearch ``_id``."""
        response = _body(
            await self.client.mget(
                index=index, ids=doc_ids, source_includes=["database_name", "belongs_to_set"]
            )
        )
        stored = {}
        for doc in response.get("docs", []):
            source = doc.get("_source") or {}
            if not doc.get("found") or source.get("database_name") != self.database_name:
                continue
            if source.get("belongs_to_set"):
                stored[doc["_id"]] = list(source["belongs_to_set"])
        return stored

    async def _bulk_index(self, index: str, documents: list[dict]) -> None:
        """Write *documents*, merging ``belongs_to_set`` with what each row already carries.

        Re-indexing a data point (the same entity reached from another node set)
        must not drop the tags it already has, as in cognee core's LanceDB and
        pgvector adapters: duplicates within the batch and the stored row are
        unioned. The read and the write share VECTOR_DB_LOCK so concurrent
        writers in this process cannot interleave between them.
        """
        merged: dict[str, dict] = {}
        for document in documents:
            doc_id = self._doc_id(document["id"])
            previous = merged.get(doc_id)
            if previous is not None:
                document["belongs_to_set"] = list(
                    dict.fromkeys(previous["belongs_to_set"] + document["belongs_to_set"])
                )
            merged[doc_id] = document
        if not merged:
            return

        async with self.VECTOR_DB_LOCK:
            stored = await self._stored_belongs_to_set(index, list(merged))
            operations = []
            for doc_id, document in merged.items():
                tags = list(dict.fromkeys(stored.get(doc_id, []) + document["belongs_to_set"]))
                document["belongs_to_set"] = tags
                payload = document.get("payload")
                if isinstance(payload, dict) and ("belongs_to_set" in payload or tags):
                    payload["belongs_to_set"] = tags
                operations.append({"index": {"_index": index, "_id": doc_id}})
                operations.append(document)
            await self._bulk(operations)

    async def _managed_indices(self) -> dict[str, dict]:
        """Every index under the prefix, mapped to its index body (settings, mappings)."""
        try:
            response = await self.client.indices.get(index=f"{self.index_prefix}-*")
        except NotFoundError:
            return {}
        return dict(_body(response))

    # ------------------------------------------------------------ collections

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        return bool(await self.client.indices.exists(index=self._index(collection_name)))

    async def create_collection(self, collection_name: str, payload_schema=None):
        async with self.VECTOR_DB_LOCK:
            if await self.has_collection(collection_name):
                return
            mappings = {
                "_meta": {"collection_name": collection_name, "managed_by": "cognee"},
                "dynamic": False,
                "properties": {
                    "id": {"type": "keyword"},
                    "database_name": {"type": "keyword"},
                    "belongs_to_set": {"type": "keyword"},
                    # Stored in _source for retrieval, never indexed: payloads
                    # are free-form and would otherwise explode the mapping.
                    "payload": {"type": "object", "enabled": False},
                    "vector": {
                        "type": "dense_vector",
                        "dims": self.embedding_engine.get_vector_size(),
                        "index": True,
                        "similarity": "cosine",
                    },
                },
            }
            try:
                await self.client.indices.create(
                    index=self._index(collection_name), mappings=mappings
                )
            except BadRequestError as error:
                if "resource_already_exists_exception" not in str(error):
                    raise

    async def create_vector_index(self, index_name: str, index_property_name: str):
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def get_collection_names(self) -> list[str]:
        """Collections holding at least one row of this dataset."""
        names = []
        for index, body in (await self._managed_indices()).items():
            count = _body(
                await self.client.count(
                    index=index, query={"bool": {"filter": [self._dataset_filter()]}}
                )
            )["count"]
            if count == 0:
                continue
            meta = (body.get("mappings") or {}).get("_meta") or {}
            names.append(meta.get("collection_name") or index[len(self.index_prefix) + 1 :])
        return names

    # ------------------------------------------------------------- data points

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        if not data_points:
            return
        if not await self.has_collection(collection_name):
            await self.create_collection(collection_name, type(data_points[0]))
        vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )
        documents = [
            self._document(data_point.id, data_point.model_dump(mode="json"), vector)
            for data_point, vector in zip(data_points, vectors, strict=True)
        ]
        await self._bulk_index(self._index(collection_name), documents)

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ):
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                IndexSchema(
                    id=data_point.id,
                    text=DataPoint.get_embeddable_data(data_point),
                    # Reference scalars, read with getattr as cognee core does so
                    # non-chunk data points fall back to None instead of raising.
                    document_id=getattr(data_point, "document_id", None),
                    document_name=getattr(data_point, "document_name", None),
                    chunk_index=getattr(data_point, "chunk_index", None),
                    source_chunk_id=getattr(data_point, "source_chunk_id", None),
                    importance_weight=getattr(data_point, "importance_weight", None),
                    external_metadata=getattr(data_point, "external_metadata", None),
                    belongs_to_set=(data_point.belongs_to_set or []),
                )
                for data_point in data_points
            ],
        )

    async def upsert_raw_vectors(
        self,
        collection_name: str,
        points: list[dict],
        payload_schema: Any | None = None,
    ) -> None:
        """Upsert caller-provided vectors without invoking the embedding engine."""
        if not points:
            return
        vector_size = self.embedding_engine.get_vector_size()
        if not await self.has_collection(collection_name):
            await self.create_collection(collection_name, payload_schema)
        documents = []
        for point in points:
            point_id = point.get("id")
            vector = point.get("vector")
            payload = point.get("payload")
            if point_id is None:
                raise ValueError("Raw vector point is missing id")
            if not isinstance(vector, list):
                raise ValueError("Raw vector point vector must be a list")
            if len(vector) != vector_size:
                raise ValueError(
                    f"Raw vector size {len(vector)} does not match expected size {vector_size}"
                )
            if payload_schema is not None:
                payload = payload_schema.model_validate(payload).model_dump(mode="json")
            documents.append(self._document(point_id, dict(payload or {}), vector))
        await self._bulk_index(self._index(collection_name), documents)

    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        if not data_point_ids:
            return []
        if not await self.has_collection(collection_name):
            return []
        ids = list(dict.fromkeys(str(point_id) for point_id in data_point_ids))
        response = _body(
            await self.client.mget(
                index=self._index(collection_name),
                ids=[self._doc_id(point_id) for point_id in ids],
                source_includes=["id", "database_name", "payload"],
            )
        )
        results = []
        for doc in response.get("docs", []):
            if not doc.get("found"):
                continue
            source = doc["_source"]
            if source.get("database_name") != self.database_name:
                continue
            results.append(
                ScoredResult(id=parse_id(source["id"]), payload=source.get("payload"), score=0)
            )
        return results

    async def update_payload(
        self, collection_name: str, payload_updates: dict[str, dict[str, Any]]
    ) -> None:
        """Overwrite payload fields on stored rows; vectors are untouched, missing ids skipped.

        Every field must already exist in the payload schema (the cognee interface
        contract). Payloads are stored unmapped, so the schema is read from the
        rows themselves: a field missing from a stored row's payload raises
        ``ValueError`` before anything is written, instead of drifting into it.
        """
        if not payload_updates:
            return
        if not await self.has_collection(collection_name):
            return
        index = self._index(collection_name)
        stored = _body(
            await self.client.mget(
                index=index,
                ids=[self._doc_id(point_id) for point_id in payload_updates],
                source_includes=["database_name", "payload"],
            )
        )
        unknown_fields = set()
        for fields, doc in zip(payload_updates.values(), stored.get("docs", []), strict=True):
            source = doc.get("_source") or {}
            if not doc.get("found") or source.get("database_name") != self.database_name:
                continue  # missing ids are skipped, not refused
            unknown_fields |= set(fields) - set(source.get("payload") or {})
        if unknown_fields:
            raise ValueError(
                f"update_payload: fields {sorted(unknown_fields)} do not exist in the "
                f"payload schema of collection {collection_name!r}"
            )
        operations = []
        for point_id, fields in payload_updates.items():
            doc: dict[str, Any] = {"payload": dict(fields)}
            if "belongs_to_set" in fields:
                doc["belongs_to_set"] = [str(tag) for tag in (fields["belongs_to_set"] or [])]
            operations.append({"update": {"_index": index, "_id": self._doc_id(point_id)}})
            operations.append({"doc": doc})
        await self._bulk(operations, ignore=("document_missing_exception",))

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        if not data_point_ids:
            return
        if not await self.has_collection(collection_name):
            return
        doc_ids = [
            self._doc_id(point_id)
            for point_id in dict.fromkeys(str(point_id) for point_id in data_point_ids)
        ]
        await self.client.delete_by_query(
            index=self._index(collection_name),
            query={"bool": {"filter": [self._dataset_filter(), {"ids": {"values": doc_ids}}]}},
            refresh=True,
            conflicts="proceed",
        )

    async def remove_belongs_to_set_tags(
        self,
        tags: list[str],
        node_ids: list[str] | None = None,
    ) -> None:
        if not tags:
            return
        if node_ids is not None and not node_ids:
            return
        tag_list = list(dict.fromkeys(str(tag) for tag in tags))
        filters = [self._dataset_filter(), {"terms": {"belongs_to_set": tag_list}}]
        if node_ids is not None:
            doc_ids = [
                self._doc_id(node_id)
                for node_id in dict.fromkeys(str(node_id) for node_id in node_ids)
            ]
            filters.append({"ids": {"values": doc_ids}})
        for index in await self._managed_indices():
            await self.client.update_by_query(
                index=index,
                query={"bool": {"filter": filters}},
                script={"source": REMOVE_TAGS_SCRIPT, "params": {"tags": tag_list}},
                refresh=True,
                conflicts="proceed",
            )

    # ------------------------------------------------------------------ search

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int | None = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
        node_name_filter_operator: str = "OR",
    ) -> list[ScoredResult]:
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()
        if not await self.has_collection(collection_name):
            raise CollectionNotFoundError(f"Collection '{collection_name}' not found!")
        if query_vector is None:
            query_vector = (await self.embed_data([query_text]))[0]

        index = self._index(collection_name)
        filters = self._filters(node_name, node_name_filter_operator)
        try:
            k = await self._resolve_limit(index, limit, filters)
            if k == 0:
                return []
            response = _body(
                await self.client.search(
                    index=index,
                    knn=self._knn(query_vector, k, filters),
                    size=k,
                    source_includes=self._source_fields(include_payload),
                )
            )
        except NotFoundError as error:
            raise CollectionNotFoundError(f"Collection '{collection_name}' not found!") from error
        return [self._scored(hit, include_payload) for hit in response["hits"]["hits"]]

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
        node_name_filter_operator: str = "OR",
    ) -> list[list[ScoredResult]]:
        """One result list per query, from a single multi-search round trip."""
        if not query_texts:
            return []
        if not await self.has_collection(collection_name):
            return [[] for _ in query_texts]

        index = self._index(collection_name)
        filters = self._filters(node_name, node_name_filter_operator)
        try:
            k = await self._resolve_limit(index, limit, filters)
        except NotFoundError:
            return [[] for _ in query_texts]
        if k == 0:
            return [[] for _ in query_texts]

        query_vectors = await self.embed_data(query_texts)
        searches = []
        for vector in query_vectors:
            searches.append({"index": index})
            searches.append(
                {
                    "knn": self._knn(vector, k, filters),
                    "size": k,
                    "_source": self._source_fields(include_payload),
                }
            )
        response = _body(await self.client.msearch(searches=searches))

        results = []
        for item in response["responses"]:
            error = item.get("error")
            if error:
                if error.get("type") == "index_not_found_exception":
                    results.append([])
                    continue
                raise RuntimeError(
                    f"Elasticsearch batch search failed: {error.get('type')}: {error.get('reason')}"
                )
            results.append([self._scored(hit, include_payload) for hit in item["hits"]["hits"]])
        return results

    async def score_by_ids(
        self, collection_name: str, data_point_ids: list[str], query_vector: list[float]
    ) -> list[ScoredResult]:
        """Exact cosine distance of the requested rows only, no ANN cutoff."""
        ids = list(dict.fromkeys(str(point_id) for point_id in data_point_ids))
        if not ids:
            return []
        if not await self.has_collection(collection_name):
            return []
        index = self._index(collection_name)
        scores = []
        for start in range(0, len(ids), SCORE_ID_BATCH_SIZE):
            batch = ids[start : start + SCORE_ID_BATCH_SIZE]
            response = _body(
                await self.client.search(
                    index=index,
                    query={
                        "script_score": {
                            "query": {
                                "bool": {
                                    "filter": [
                                        self._dataset_filter(),
                                        {"ids": {"values": [self._doc_id(i) for i in batch]}},
                                    ]
                                }
                            },
                            # script_score must stay non-negative, hence the +1.
                            "script": {
                                "source": "cosineSimilarity(params.query_vector, 'vector') + 1.0",
                                "params": {"query_vector": query_vector},
                            },
                        }
                    },
                    size=len(batch),
                    source_includes=["id"],
                )
            )
            scores.extend(
                ScoredResult(
                    id=parse_id(hit["_source"]["id"]),
                    score=2.0 - float(hit["_score"]),
                    payload=None,
                )
                for hit in response["hits"]["hits"]
            )
        return scores

    # ------------------------------------------------------------------- prune

    async def prune(self):
        """Delete this dataset's rows everywhere; drop indices left empty."""
        for index in await self._managed_indices():
            await self.client.delete_by_query(
                index=index,
                query={"bool": {"filter": [self._dataset_filter()]}},
                refresh=True,
                conflicts="proceed",
            )
            remaining = _body(await self.client.count(index=index))["count"]
            if remaining == 0:
                await self.client.indices.delete(index=index, ignore_unavailable=True)
