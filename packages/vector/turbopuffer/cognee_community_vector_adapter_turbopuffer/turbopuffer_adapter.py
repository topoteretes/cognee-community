import asyncio
from datetime import datetime
from typing import List, Optional, Union, get_args, get_origin
from uuid import UUID

import turbopuffer
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

logger = get_logger("TurbopufferAdapter")

# Turbopuffer filterable attribute size limit is 4096 bytes.
# Fields larger than this should not be stored as attributes or should be truncated.
_MAX_ATTR_BYTES = 4096


class IndexSchema(DataPoint):
    text: str

    # Reference scalars carried into the indexed payload, mirroring the
    # pgvector/lancedb index schema. source_chunk_id is required by hybrid
    # search to pair a TextSummary vector hit back to its source chunk;
    # without it the hybrid summary leg is silently dropped.
    source_chunk_id: Optional[str] = None
    document_id: Optional[str] = None
    document_name: Optional[str] = None
    chunk_index: Optional[int] = None
    importance_weight: Optional[float] = None

    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: List[str] = []


def _str_or_none(value):
    """Coerce ids (UUID/str) to str for the index payload, preserving None."""
    return None if value is None else str(value)


def _serialize_value(value):
    """Recursively serialize complex types to turbopuffer-compatible values."""
    if isinstance(value, datetime):
        return value.isoformat()
    elif isinstance(value, UUID):
        return str(value)
    elif isinstance(value, dict):
        return str({k: _serialize_value(v) for k, v in value.items()})
    elif isinstance(value, list):
        # Keep lists as native arrays for ContainsAny filtering support
        serialized = [_serialize_value(v) for v in value]
        # Only keep as list if all elements are strings
        if all(isinstance(v, str) for v in serialized):
            return serialized
        return str(serialized)
    return value


def _coerce_vector(value):
    """Convert vector-like values (including numpy arrays) to list[float]."""
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        value = value.tolist()

    if isinstance(value, tuple):
        value = list(value)

    if not isinstance(value, list):
        raise TypeError(f"Vector must be a list-like value, got {type(value).__name__}")

    return [float(v) for v in value]


def _truncate_large_values(payload: dict) -> dict:
    """Truncate string values exceeding turbopuffer's filterable attribute limit."""
    result = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_ATTR_BYTES:
            # Truncate to fit within limit
            encoded = value.encode("utf-8")[: _MAX_ATTR_BYTES - 3]
            result[key] = encoded.decode("utf-8", errors="ignore") + "..."
        else:
            result[key] = value
    return result


def _strip_optional(annotation):
    origin = get_origin(annotation)
    if origin is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _annotation_to_turbopuffer_type(annotation):
    annotation = _strip_optional(annotation)
    origin = get_origin(annotation)

    if origin in (list, List):
        args = get_args(annotation)
        if len(args) != 1:
            return None
        item_type = _strip_optional(args[0])
        if item_type is str:
            return "[]string"
        if item_type is int:
            return "[]int"
        if item_type is float:
            return "[]float"
        if item_type is bool:
            return "[]bool"
        if item_type is UUID:
            return "[]uuid"
        if item_type is datetime:
            return "[]datetime"
        return None

    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is str:
        return "string"
    if annotation is UUID:
        return "uuid"
    if annotation is datetime:
        return "datetime"

    return None


def _infer_turbopuffer_attribute_type(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return "[]string"
        if all(isinstance(item, bool) for item in value):
            return "[]bool"
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            return "[]int"
        if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
            return "[]float"
    return None


def _merge_turbopuffer_types(existing_type: str, new_type: str) -> str:
    if existing_type == new_type:
        return existing_type

    # Promote numeric types to avoid int schemas rejecting float values later.
    if {existing_type, new_type} == {"int", "float"}:
        return "float"
    if {existing_type, new_type} == {"[]int", "[]float"}:
        return "[]float"

    # Keep existing type if no safe merge is known.
    return existing_type


def _build_write_schema(data_points: list[DataPoint], rows: list[dict]) -> dict:
    schema = {}

    # First use model annotations so schema is stable and future fields are auto-covered.
    for data_point in data_points:
        for key, field in data_point.model_fields.items():
            inferred_type = _annotation_to_turbopuffer_type(field.annotation)
            if inferred_type is None:
                continue
            if key not in schema:
                schema[key] = inferred_type
            else:
                schema[key] = _merge_turbopuffer_types(schema[key], inferred_type)

    # Then use actual row values for non-model fields and to widen numeric types when needed.
    for row in rows:
        for key, value in row.items():
            if key in ("id", "vector"):
                continue
            if value is None:
                continue

            inferred_type = _infer_turbopuffer_attribute_type(value)
            if inferred_type is None:
                continue
            if key not in schema:
                schema[key] = inferred_type
            else:
                schema[key] = _merge_turbopuffer_types(schema[key], inferred_type)

    return schema


class TurbopufferAdapter(VectorDBInterface):
    name = "Turbopuffer"

    def __init__(
        self,
        url=None,
        api_key=None,
        embedding_engine: EmbeddingEngine = None,
        database_name: str = "cognee_db",
    ):
        self.embedding_engine = embedding_engine
        self.database_name = database_name
        self.url = url
        self.api_key = api_key
        self.VECTOR_DB_LOCK = asyncio.Lock()
        self._client = None

    def _get_client(self) -> turbopuffer.Turbopuffer:
        if self._client is None:
            kwargs = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.url:
                kwargs["region"] = self.url
            self._client = turbopuffer.Turbopuffer(**kwargs)
        return self._client

    def _namespace_name(self, collection_name: str) -> str:
        return f"{self.database_name}_{collection_name}"

    def _get_namespace(self, collection_name: str):
        client = self._get_client()
        return client.namespace(self._namespace_name(collection_name))

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        try:
            ns = self._get_namespace(collection_name)
            result = await asyncio.to_thread(ns.exists)
            return result
        except Exception:
            return False

    async def create_collection(
        self,
        collection_name: str,
        payload_schema=None,
    ):
        # Turbopuffer namespaces are created implicitly on first write.
        pass

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        if not data_points:
            return

        ns = self._get_namespace(collection_name)

        data_vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )

        rows = []
        for i, data_point in enumerate(data_points):
            payload = data_point.model_dump()
            for key, value in list(payload.items()):
                payload[key] = _serialize_value(value)

            payload = _truncate_large_values(payload)

            row = {
                "id": str(data_point.id),
                "vector": _coerce_vector(data_vectors[i]),
                "database_name": self.database_name,
                **payload,
            }
            rows.append(row)

        write_schema = _build_write_schema(data_points, rows)


        try:
            await asyncio.to_thread(
                ns.write,
                upsert_rows=rows,
                distance_metric="cosine_distance",
                schema=write_schema,
            )
        except Exception as error:
            error_msg = str(error)
            if "not found" in error_msg.lower():
                raise CollectionNotFoundError(
                    message=f"Collection {collection_name} not found!"
                ) from error
            logger.error("Error uploading data points to Turbopuffer: %s", error_msg)
            raise

    async def create_vector_index(self, index_name: str, index_property_name: str):
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ):
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                IndexSchema(
                    id=data_point.id,
                    text=getattr(data_point, data_point.metadata["index_fields"][0]),
                    # Pulled via getattr so non-summary/non-chunk data points
                    # (which lack these fields) fall back to None.
                    source_chunk_id=_str_or_none(getattr(data_point, "source_chunk_id", None)),
                    document_id=_str_or_none(getattr(data_point, "document_id", None)),
                    document_name=getattr(data_point, "document_name", None),
                    chunk_index=getattr(data_point, "chunk_index", None),
                    importance_weight=getattr(data_point, "importance_weight", None),
                    belongs_to_set=(data_point.belongs_to_set or []),
                )
                for data_point in data_points
            ],
        )

    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        if not await self.has_collection(collection_name):
            return []

        ns = self._get_namespace(collection_name)
        str_ids = [str(id) for id in data_point_ids]

        try:
            response = await asyncio.to_thread(
                ns.query,
                filters=("Or", tuple(("id", "Eq", id) for id in str_ids)),
                top_k=len(str_ids),
                include_attributes=True,
            )
            results = []
            for row in response.rows or []:
                extra = row.model_extra or {}
                payload = {k: v for k, v in extra.items() if k not in ("$dist",)}
                payload["id"] = parse_id(str(row.id))
                results.append(
                    ScoredResult(
                        id=parse_id(str(row.id)),
                        payload=payload,
                        score=0,
                    )
                )
            return results
        except Exception as e:
            logger.error("Error retrieving data points from Turbopuffer: %s", str(e))
            return []

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int | None = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> list[ScoredResult]:
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if not await self.has_collection(collection_name):
            return []

        if query_vector is None:
            query_vector = (await self.embed_data([query_text]))[0]
        query_vector = _coerce_vector(query_vector)

        if limit is None:
            limit = 100
        if limit == 0:
            return []

        ns = self._get_namespace(collection_name)

        try:
            filters = ("database_name", "Eq", self.database_name)

            if node_name:
                # belongs_to_set is stored as a native array, use ContainsAny for filtering
                if node_name_filter_operator == "AND":
                    all_filters = [filters] + [
                        ("belongs_to_set", "ContainsAny", [name]) for name in node_name
                    ]
                    filters = ("And", tuple(all_filters))
                else:
                    node_filter = ("belongs_to_set", "ContainsAny", node_name)
                    filters = ("And", (filters, node_filter))

            query_kwargs = {
                "rank_by": ("vector", "ANN", query_vector),
                "top_k": limit,
                "filters": filters,
            }
            if include_payload:
                query_kwargs["include_attributes"] = True

            response = await asyncio.to_thread(ns.query, **query_kwargs)

            rows = response.rows or []
            if not rows:
                return []

            # Normalize distances to [0, 1] consistent with core adapters
            distances = [row["$dist"] if "$dist" in row else 0.0 for row in rows]
            min_dist = min(distances)
            max_dist = max(distances)
            if max_dist == min_dist:
                normalized = [0.0] * len(distances)
            else:
                normalized = [(d - min_dist) / (max_dist - min_dist) for d in distances]

            scored_results = []
            for i, row in enumerate(rows):
                payload = None
                if include_payload:
                    extra = row.model_extra or {}
                    payload = {k: v for k, v in extra.items() if k not in ("$dist",)}
                    payload["id"] = parse_id(str(row.id))

                scored_results.append(
                    ScoredResult(
                        id=parse_id(str(row.id)),
                        payload=payload,
                        score=normalized[i],
                    )
                )

            return scored_results
        except Exception as e:
            logger.error(f"Error in Turbopuffer search: {e}", exc_info=True)
            return []

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ):
        if not query_texts:
            return []

        if limit is None:
            limit = 100
        if limit == 0:
            return []

        all_results = []
        for query_text in query_texts:
            results = await self.search(
                collection_name=collection_name,
                query_text=query_text,
                limit=limit,
                with_vector=with_vectors,
                include_payload=include_payload,
                node_name=node_name,
                node_name_filter_operator=node_name_filter_operator,
            )
            all_results.append(results)

        return all_results

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        if not await self.has_collection(collection_name):
            return

        ns = self._get_namespace(collection_name)
        str_ids = [str(id) for id in data_point_ids]

        try:
            await asyncio.to_thread(ns.write, deletes=str_ids)
        except Exception as e:
            logger.error("Error deleting data points from Turbopuffer: %s", str(e))
            raise

    async def _list_namespaces_with_prefix(self) -> list[str]:
        """List all namespace names matching the database_name prefix."""
        client = self._get_client()
        prefix = f"{self.database_name}_"
        names = []
        page = await asyncio.to_thread(client.namespaces, prefix=prefix)
        while True:
            for ns_info in page.namespaces:
                ns_name = ns_info.id
                if ns_name.startswith(prefix):
                    names.append(ns_name)
            if not page.has_next_page():
                break
            page = await asyncio.to_thread(page.get_next_page)
        return names

    async def prune(self):
        client = self._get_client()
        try:
            ns_names = await self._list_namespaces_with_prefix()
            for ns_name in ns_names:
                ns = client.namespace(ns_name)
                await asyncio.to_thread(ns.delete_all)
        except Exception as e:
            logger.error("Error pruning Turbopuffer namespaces: %s", str(e))
            raise

    async def get_collection_names(self) -> list[str]:
        """Get names of all collections in the database."""
        prefix = f"{self.database_name}_"
        prefix_len = len(prefix)

        try:
            ns_names = await self._list_namespaces_with_prefix()
            return [name[prefix_len:] for name in ns_names]
        except Exception as e:
            logger.error("Error listing Turbopuffer namespaces: %s", str(e))
            return []
