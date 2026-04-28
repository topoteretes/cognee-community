import asyncio
import base64
import hashlib
import json
import math
import os
import re
from typing import Any, List, Optional
from urllib.parse import unquote, urlparse
from uuid import UUID

import singlestoredb as s2
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.shared.logging_utils import get_logger

logger = get_logger("SingleStoreAdapter")


class SingleStoreDataPoint(DataPoint):
    text: str
    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: List[str] = []


class VectorEngineInitializationError(Exception):
    pass


def serialize_for_json(obj: Any) -> Any:
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, dict):
        return {key: serialize_for_json(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [serialize_for_json(item) for item in obj]
    return obj


def safe_parse_id(value: str) -> Any:
    try:
        return parse_id(value)
    except Exception:
        return value


class SingleStoreAdapter(VectorDBInterface):
    name = "SingleStore"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        database_name: str = "cognee",
        embedding_engine: EmbeddingEngine | None = None,
        endpoint: str | None = None,
        **kwargs: dict | None,
    ):
        if embedding_engine is None:
            raise VectorEngineInitializationError("Embedding engine is required!")

        self.embedding_engine = embedding_engine
        self.database_name = database_name or "cognee"
        self.table_prefix = self._sanitize_prefix(
            os.getenv("COGNEE_SINGLESTORE_TABLE_PREFIX", "cognee_vec_")
        )
        self.normalize_vectors = self._env_flag("COGNEE_SINGLESTORE_NORMALIZE_VECTORS", True)
        self.VECTOR_DB_LOCK = asyncio.Lock()

        self.connection_config = self._build_connection_config(
            endpoint or url or kwargs.get("utl") or os.getenv("VECTOR_DB_URL"),
            api_key or os.getenv("VECTOR_DB_KEY"),
        )

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        table_name = self._table_name(collection_name)
        result = await self._fetch_one(
            """
            SELECT COUNT(*) AS table_count
            FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = %s
            """,
            (table_name,),
        )
        return bool(result and int(result["table_count"]) > 0)

    async def create_collection(self, collection_name: str, payload_schema: Any | None = None):
        async with self.VECTOR_DB_LOCK:
            table_name = self._table_name(collection_name)
            vector_size = self.embedding_engine.get_vector_size()
            await self._execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._quote_identifier(table_name)} (
                    database_name VARCHAR(255) NOT NULL,
                    id VARCHAR(64) NOT NULL,
                    payload JSON,
                    vector VECTOR({int(vector_size)}, F32) NOT NULL,
                    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
                    updated_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)
                        ON UPDATE CURRENT_TIMESTAMP(6),
                    PRIMARY KEY (database_name, id),
                    SHARD KEY (database_name, id),
                    SORT KEY ()
                )
                """
            )

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]) -> None:
        if not data_points:
            return

        if not await self.has_collection(collection_name):
            await self.create_collection(collection_name, type(data_points[0]))

        vectors = await self.embed_data([DataPoint.get_embeddable_data(dp) for dp in data_points])
        table_name = self._table_name(collection_name)
        rows = []

        for data_point, vector in zip(data_points, vectors, strict=False):
            payload = serialize_for_json(data_point.model_dump())
            payload["database_name"] = self.database_name
            vector_json = json.dumps(self._normalize_vector(vector), separators=(",", ":"))
            rows.append(
                (
                    self.database_name,
                    str(data_point.id),
                    json.dumps(payload, separators=(",", ":")),
                    vector_json,
                )
            )

        await self._executemany(
            f"""
            INSERT INTO {self._quote_identifier(table_name)}
                (database_name, id, payload, vector)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                payload = VALUES(payload),
                vector = VALUES(vector),
                updated_at = NOW(6)
            """,
            rows,
        )

    async def create_vector_index(self, index_name: str, index_property_name: str):
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ):
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                SingleStoreDataPoint(
                    id=data_point.id,
                    text=getattr(
                        data_point,
                        data_point.metadata.get("index_fields", ["text"])[0],
                    ),
                    belongs_to_set=(data_point.belongs_to_set or []),
                )
                for data_point in data_points
            ],
        )

    async def retrieve(self, collection_name: str, data_point_ids: list[str]) -> list[dict[str, Any]]:
        if not data_point_ids or not await self.has_collection(collection_name):
            return []

        table_name = self._table_name(collection_name)
        placeholders = ", ".join(["%s"] * len(data_point_ids))
        rows = await self._fetch_all(
            f"""
            SELECT id, payload
            FROM {self._quote_identifier(table_name)}
            WHERE database_name = %s AND id IN ({placeholders})
            """,
            (self.database_name, *[str(data_id) for data_id in data_point_ids]),
        )
        payload_by_id = {
            str(row["id"]): self._payload_with_id(row["payload"], str(row["id"])) for row in rows
        }
        return [
            payload_by_id[str(data_id)]
            for data_id in data_point_ids
            if str(data_id) in payload_by_id
        ]

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

        if limit is None:
            limit = await self._collection_size(collection_name)
        if limit <= 0:
            return []

        table_name = self._table_name(collection_name)
        query_vector_json = json.dumps(
            self._normalize_vector(query_vector), separators=(",", ":")
        )
        select_payload = ", payload" if include_payload else ""
        select_vector = ", vector" if with_vector else ""
        filter_sql, filter_params = self._build_filter(node_name, node_name_filter_operator)

        rows = await self._fetch_all(
            f"""
            SELECT id{select_payload}{select_vector},
                   DOT_PRODUCT(vector, %s) AS score
            FROM {self._quote_identifier(table_name)}
            WHERE database_name = %s{filter_sql}
            ORDER BY score DESC
            LIMIT %s
            """,
            (query_vector_json, self.database_name, *filter_params, int(limit)),
        )

        return [
            ScoredResult(
                id=safe_parse_id(str(row["id"])),
                payload=self._payload_with_id(row["payload"], str(row["id"]))
                if include_payload
                else None,
                score=float(row["score"] or 0.0),
            )
            for row in rows
        ]

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = 15,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> list[list[ScoredResult]]:
        vectors = await self.embed_data(query_texts)
        return await asyncio.gather(
            *[
                self.search(
                    collection_name=collection_name,
                    query_vector=vector,
                    limit=limit,
                    with_vector=with_vectors,
                    include_payload=include_payload,
                    node_name=node_name,
                    node_name_filter_operator=node_name_filter_operator,
                )
                for vector in vectors
            ]
        )

    async def delete_data_points(
        self, collection_name: str, data_point_ids: list[str]
    ) -> dict[str, int]:
        if not data_point_ids or not await self.has_collection(collection_name):
            return {"deleted": 0}

        table_name = self._table_name(collection_name)
        placeholders = ", ".join(["%s"] * len(data_point_ids))
        deleted_count = await self._execute(
            f"""
            DELETE FROM {self._quote_identifier(table_name)}
            WHERE database_name = %s AND id IN ({placeholders})
            """,
            (self.database_name, *[str(data_id) for data_id in data_point_ids]),
        )
        return {"deleted": int(deleted_count)}

    async def prune(self) -> None:
        for table_name in await self._list_prefixed_tables():
            quoted_table_name = self._quote_identifier(table_name)
            await self._execute(
                f"DELETE FROM {quoted_table_name} WHERE database_name = %s",
                (self.database_name,),
            )
            remaining = await self._fetch_one(
                f"SELECT COUNT(*) AS row_count FROM {quoted_table_name}"
            )
            if remaining and int(remaining["row_count"]) == 0:
                await self._execute(f"DROP TABLE IF EXISTS {quoted_table_name}")

    async def get_collection_names(self) -> list[str]:
        collection_names = []
        for table_name in await self._list_prefixed_tables():
            result = await self._fetch_one(
                f"""
                SELECT COUNT(*) AS row_count
                FROM {self._quote_identifier(table_name)}
                WHERE database_name = %s
                """,
                (self.database_name,),
            )
            if result and int(result["row_count"]) > 0:
                collection_names.append(table_name)
        return collection_names

    async def _collection_size(self, collection_name: str) -> int:
        table_name = self._table_name(collection_name)
        result = await self._fetch_one(
            f"""
            SELECT COUNT(*) AS row_count
            FROM {self._quote_identifier(table_name)}
            WHERE database_name = %s
            """,
            (self.database_name,),
        )
        return int(result["row_count"]) if result else 0

    def _build_filter(
        self, node_name: Optional[List[str]], node_name_filter_operator: str
    ) -> tuple[str, tuple[str, ...]]:
        if not node_name:
            return "", ()

        operator = "AND" if node_name_filter_operator == "AND" else "OR"
        predicates = [
            "JSON_MATCH_ANY(MATCH_PARAM_STRING_STRICT() = %s, "
            "payload, 'belongs_to_set' MATCH_ELEMENTS)"
            for _ in node_name
        ]
        return f" AND ({f' {operator} '.join(predicates)})", tuple(node_name)

    async def _list_prefixed_tables(self) -> list[str]:
        rows = await self._fetch_all(
            """
            SELECT table_name AS table_name
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND LEFT(table_name, CHAR_LENGTH(%s)) = %s
            """,
            (self.table_prefix, self.table_prefix),
        )
        return [
            str(row["table_name"])
            for row in rows
            if str(row["table_name"]).startswith(self.table_prefix)
        ]

    async def _execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        return await asyncio.to_thread(self._execute_sync, sql, params)

    async def _executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> int:
        return await asyncio.to_thread(self._executemany_sync, sql, rows)

    async def _fetch_one(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> dict[str, Any] | None:
        rows = await self._fetch_all(sql, params)
        return rows[0] if rows else None

    async def _fetch_all(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._fetch_all_sync, sql, params)

    def _execute_sync(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params or ())
                row_count = cursor.rowcount
            connection.commit()
            return row_count
        finally:
            connection.close()

    def _executemany_sync(self, sql: str, rows: list[tuple[Any, ...]]) -> int:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.executemany(sql, rows)
                row_count = cursor.rowcount
            connection.commit()
            return row_count
        finally:
            connection.close()

    def _fetch_all_sync(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params or ())
                rows = cursor.fetchall()
            return list(rows)
        finally:
            connection.close()

    def _connect(self):
        connection = s2.connect(**self.connection_config)
        with connection.cursor() as cursor:
            cursor.execute("SET vector_type_project_format = JSON")
        return connection

    def _build_connection_config(self, url: str | None, api_key: str | None) -> dict[str, Any]:
        api_key_config = self._decode_api_key(api_key)
        url_config = self._parse_url(url)

        config = {
            "host": os.getenv("VECTOR_DB_HOST")
            or os.getenv("SINGLESTORE_HOST")
            or url_config.get("host")
            or "localhost",
            "port": int(
                os.getenv("VECTOR_DB_PORT")
                or os.getenv("SINGLESTORE_PORT")
                or url_config.get("port")
                or 3306
            ),
            "user": os.getenv("VECTOR_DB_USERNAME")
            or os.getenv("SINGLESTORE_USERNAME")
            or api_key_config.get("username")
            or url_config.get("user")
            or "root",
            "password": os.getenv("VECTOR_DB_PASSWORD")
            or os.getenv("SINGLESTORE_PASSWORD")
            or api_key_config.get("password")
            or url_config.get("password")
            or api_key_config.get("api_key")
            or "",
            "database": os.getenv("VECTOR_DB_NAME")
            or os.getenv("SINGLESTORE_DATABASE")
            or api_key_config.get("database")
            or url_config.get("database")
            or "cognee",
            "autocommit": False,
            "results_type": "dicts",
        }

        for key in ("ssl_disabled", "ssl_ca", "ssl_cert", "ssl_key"):
            if key in api_key_config:
                config[key] = api_key_config[key]

        return config

    def _parse_url(self, raw_url: str | None) -> dict[str, Any]:
        if not raw_url:
            return {}

        if "://" not in raw_url:
            if "@" in raw_url or "/" in raw_url:
                raw_url = f"singlestoredb://{raw_url}"
            else:
                host, _, port = raw_url.partition(":")
                return {"host": host, "port": int(port) if port else None}

        parsed = urlparse(raw_url)
        database = parsed.path.lstrip("/").split("/", 1)[0] if parsed.path else None
        return {
            "host": parsed.hostname,
            "port": parsed.port,
            "user": unquote(parsed.username) if parsed.username else None,
            "password": unquote(parsed.password) if parsed.password else None,
            "database": database or None,
        }

    def _decode_api_key(self, api_key: str | None) -> dict[str, Any]:
        if not api_key:
            return {}

        for candidate in (api_key, self._try_base64_decode(api_key)):
            if not candidate:
                continue
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded

        return {"api_key": api_key}

    def _try_base64_decode(self, value: str) -> str | None:
        try:
            return base64.b64decode(value).decode("utf-8")
        except Exception:
            return None

    def _table_name(self, collection_name: str) -> str:
        normalized = collection_name.lower()
        sanitized = re.sub(r"[^a-z0-9_]+", "_", normalized).strip("_") or "collection"
        needs_hash = sanitized != normalized or len(self.table_prefix + sanitized) > 64
        suffix = ""
        if needs_hash:
            suffix = "_" + hashlib.sha1(collection_name.encode("utf-8")).hexdigest()[:8]

        max_collection_length = 64 - len(self.table_prefix) - len(suffix)
        return f"{self.table_prefix}{sanitized[:max_collection_length]}{suffix}"

    def _sanitize_prefix(self, prefix: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", prefix)
        return (sanitized or "cognee_vec_")[:48]

    def _quote_identifier(self, identifier: str) -> str:
        return f"`{identifier.replace('`', '``')}`"

    def _normalize_vector(self, vector: list[float]) -> list[float]:
        values = [float(value) for value in vector]
        if not self.normalize_vectors:
            return values

        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            return values
        return [value / norm for value in values]

    def _payload_with_id(self, payload: Any, data_point_id: str) -> dict[str, Any]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        elif payload is None:
            payload = {}

        if not isinstance(payload, dict):
            payload = {"payload": payload}
        payload["id"] = safe_parse_id(data_point_id)
        return payload

    def _env_flag(self, name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}
