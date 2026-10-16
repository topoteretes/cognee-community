import asyncio
import base64
import hashlib
import json
import math
import os
import re
import time
from typing import Any, Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urlparse
from uuid import UUID

import clickhouse_connect
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.shared.logging_utils import get_logger

logger = get_logger("ClickHouseAdapter")


class ClickHouseDataPoint(DataPoint):
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


class ClickHouseAdapter(VectorDBInterface):
    name = "ClickHouse"

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
            os.getenv("COGNEE_CLICKHOUSE_TABLE_PREFIX", "cognee_vec_")
        )
        self.normalize_vectors = self._env_flag("COGNEE_CLICKHOUSE_NORMALIZE_VECTORS", True)
        self.enable_vector_index = self._env_flag("COGNEE_CLICKHOUSE_ENABLE_VECTOR_INDEX", False)
        self.vector_index_granularity = self._env_int(
            "COGNEE_CLICKHOUSE_VECTOR_INDEX_GRANULARITY", 100000000
        )
        self.vector_index_name = self._sanitize_identifier(
            os.getenv("COGNEE_CLICKHOUSE_VECTOR_INDEX_NAME", "idx_vector")
        )
        self.VECTOR_DB_LOCK = asyncio.Lock()
        self._warned_vector_index_unsupported = False

        raw_url = (
            endpoint
            or url
            or kwargs.get("url")
            or os.getenv("CLICKHOUSE_URL")
            or os.getenv("VECTOR_DB_URL")
        )
        raw_key = api_key or os.getenv("CLICKHOUSE_KEY") or os.getenv("VECTOR_DB_KEY")
        self.connection_config = self._build_connection_config(raw_url, raw_key)

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        table_name = self._table_name(collection_name)
        result = await self._fetch_one(
            """
            SELECT count() AS table_count
            FROM system.tables
            WHERE database = currentDatabase()
              AND name = {table_name:String}
            """,
            {"table_name": table_name},
        )
        return bool(result and int(result["table_count"]) > 0)

    async def create_collection(self, collection_name: str, payload_schema: Any | None = None):
        async with self.VECTOR_DB_LOCK:
            table_name = self._table_name(collection_name)
            vector_size = self._vector_size()

            await self._execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._quote_identifier(table_name)} (
                    database_name String,
                    id String,
                    payload String,
                    text String,
                    belongs_to_set Array(String),
                    vector Array(Float32),
                    version UInt64,
                    is_deleted UInt8 DEFAULT 0,
                    created_at DateTime64(6) DEFAULT now64(6),
                    updated_at DateTime64(6) DEFAULT now64(6),
                    CONSTRAINT vector_length CHECK length(vector) = {int(vector_size)}
                )
                ENGINE = ReplacingMergeTree(version, is_deleted)
                ORDER BY (database_name, id)
                """
            )
            await self._ensure_vector_index(table_name, vector_size)

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]) -> None:
        if not data_points:
            return

        if not await self.has_collection(collection_name):
            await self.create_collection(collection_name, type(data_points[0]))

        embeddable_data = [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        vectors = await self.embed_data(embeddable_data)
        if len(vectors) != len(data_points):
            raise ValueError(
                "Embedding engine returned a different number of vectors than input data points."
            )

        expected_size = self._vector_size()
        table_name = self._table_name(collection_name)
        base_version = time.time_ns()
        rows = []

        for row_offset, (data_point, vector) in enumerate(zip(data_points, vectors, strict=False)):
            normalized_vector = self._normalize_vector(
                self._validate_vector(vector, expected_size=expected_size)
            )
            payload = serialize_for_json(data_point.model_dump())
            payload["database_name"] = self.database_name
            belongs_to_set = self._belongs_to_set(data_point)
            rows.append(
                (
                    self.database_name,
                    str(data_point.id),
                    json.dumps(payload, separators=(",", ":")),
                    self._get_text_content(data_point),
                    belongs_to_set,
                    normalized_vector,
                    base_version + row_offset,
                    0,
                )
            )

        await self._insert(
            table_name,
            rows,
            [
                "database_name",
                "id",
                "payload",
                "text",
                "belongs_to_set",
                "vector",
                "version",
                "is_deleted",
            ],
        )

    async def create_vector_index(self, index_name: str, index_property_name: str):
        collection_name = f"{index_name}_{index_property_name}"
        await self.create_collection(collection_name)
        if self.enable_vector_index:
            await self._ensure_vector_index(self._table_name(collection_name), self._vector_size())

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ):
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                ClickHouseDataPoint(
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

    async def retrieve(
        self, collection_name: str, data_point_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not data_point_ids or not await self.has_collection(collection_name):
            return []

        table_name = self._table_name(collection_name)
        rows = await self._fetch_all(
            f"""
            SELECT id, payload
            FROM {self._quote_identifier(table_name)} FINAL
            WHERE database_name = {{database_name:String}}
              AND is_deleted = 0
              AND has({{ids:Array(String)}}, id)
            """,
            {
                "database_name": self.database_name,
                "ids": [str(data_id) for data_id in data_point_ids],
            },
        )
        payload_by_id = {
            str(row["id"]): self._payload_with_id(row.get("payload"), str(row["id"]))
            for row in rows
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

        expected_size = self._vector_size()
        query_vector = self._normalize_vector(
            self._validate_vector(query_vector, expected_size=expected_size)
        )
        table_name = self._table_name(collection_name)
        filter_sql, filter_params = self._build_filter(node_name, node_name_filter_operator)
        parameters: dict[str, Any] = {
            "database_name": self.database_name,
            "limit": int(limit),
            "query_vector": query_vector,
            **filter_params,
        }
        select_payload = ", payload" if include_payload else ""
        select_vector = ", vector" if with_vector else ""
        use_vector_index = self.enable_vector_index and await self.has_vector_index(collection_name)
        score_expression = (
            "1 - cosineDistance(vector, {query_vector:Array(Float32)})"
            if use_vector_index
            else "dotProduct(vector, {query_vector:Array(Float32)})"
        )
        order_expression = (
            "cosineDistance(vector, {query_vector:Array(Float32)}) ASC"
            if use_vector_index
            else "score DESC"
        )

        rows = await self._fetch_all(
            f"""
            SELECT id{select_payload}{select_vector},
                   {score_expression} AS score
            FROM {self._quote_identifier(table_name)} FINAL
            WHERE database_name = {{database_name:String}}
              AND is_deleted = 0{filter_sql}
            ORDER BY {order_expression}
            LIMIT {{limit:UInt64}}
            """,
            parameters,
        )

        return [
            ScoredResult(
                id=safe_parse_id(str(row["id"])),
                payload=self._payload_with_id(row.get("payload"), str(row["id"]))
                if include_payload
                else None,
                score=float(row.get("score") or 0.0),
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
        if not query_texts:
            return []

        vectors = await self.embed_data(query_texts)
        if len(vectors) != len(query_texts):
            raise ValueError(
                "Embedding engine returned a different number of vectors than input queries."
            )

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
        ids = [str(data_id) for data_id in data_point_ids]
        existing = await self._fetch_one(
            f"""
            SELECT count() AS row_count
            FROM {self._quote_identifier(table_name)} FINAL
            WHERE database_name = {{database_name:String}}
              AND is_deleted = 0
              AND has({{ids:Array(String)}}, id)
            """,
            {"database_name": self.database_name, "ids": ids},
        )
        deleted_count = int(existing["row_count"]) if existing else 0
        vector_size = self._vector_size()
        base_version = time.time_ns()
        rows = [
            (
                self.database_name,
                data_id,
                "{}",
                "",
                [],
                [0.0] * vector_size,
                base_version + row_offset,
                1,
            )
            for row_offset, data_id in enumerate(ids)
        ]
        await self._insert(
            table_name,
            rows,
            [
                "database_name",
                "id",
                "payload",
                "text",
                "belongs_to_set",
                "vector",
                "version",
                "is_deleted",
            ],
        )
        return {"deleted": deleted_count}

    async def prune(self) -> None:
        for table_name in await self._list_prefixed_tables():
            quoted_table_name = self._quote_identifier(table_name)
            await self._execute(
                (
                    f"ALTER TABLE {quoted_table_name} "
                    "DELETE WHERE database_name = {database_name:String}"
                ),
                {"database_name": self.database_name},
                settings={"mutations_sync": 2},
            )
            total_remaining = await self._fetch_one(
                f"SELECT count() AS row_count FROM {quoted_table_name} FINAL "
                f"WHERE is_deleted = 0",
                {},
            )
            if total_remaining and int(total_remaining["row_count"]) == 0:
                await self._execute(f"DROP TABLE IF EXISTS {quoted_table_name}")

    async def get_collection_names(self) -> list[str]:
        collection_names = []
        for table_name in await self._list_prefixed_tables():
            result = await self._fetch_one(
                f"""
                SELECT count() AS row_count
                FROM {self._quote_identifier(table_name)} FINAL
                WHERE database_name = {{database_name:String}}
                  AND is_deleted = 0
                """,
                {"database_name": self.database_name},
            )
            if result and int(result["row_count"]) > 0:
                collection_names.append(table_name)
        return collection_names

    async def has_vector_index(self, collection_name: str) -> bool:
        table_name = self._table_name(collection_name)
        result = await self._fetch_one(
            """
            SELECT count() AS index_count
            FROM system.data_skipping_indices
            WHERE database = currentDatabase()
              AND table = {table_name:String}
              AND name = {index_name:String}
              AND type = 'vector_similarity'
            """,
            {"table_name": table_name, "index_name": self.vector_index_name},
        )
        return bool(result and int(result["index_count"]) > 0)

    async def _collection_size(self, collection_name: str) -> int:
        table_name = self._table_name(collection_name)
        result = await self._fetch_one(
            f"""
            SELECT count() AS row_count
            FROM {self._quote_identifier(table_name)} FINAL
            WHERE database_name = {{database_name:String}}
              AND is_deleted = 0
            """,
            {"database_name": self.database_name},
        )
        return int(result["row_count"]) if result else 0

    def _build_filter(
        self, node_name: Optional[List[str]], node_name_filter_operator: str
    ) -> tuple[str, dict[str, Any]]:
        if not node_name:
            return "", {}

        operator = node_name_filter_operator.upper()
        function_name = "hasAll" if operator == "AND" else "hasAny"
        return (
            f" AND {function_name}(belongs_to_set, {{node_names:Array(String)}})",
            {"node_names": [str(name) for name in node_name]},
        )

    async def _list_prefixed_tables(self) -> list[str]:
        rows = await self._fetch_all(
            """
            SELECT name AS table_name
            FROM system.tables
            WHERE database = currentDatabase()
              AND startsWith(name, {table_prefix:String})
            """,
            {"table_prefix": self.table_prefix},
        )
        return [
            str(row["table_name"])
            for row in rows
            if str(row["table_name"]).startswith(self.table_prefix)
        ]

    async def _ensure_vector_index(self, table_name: str, vector_size: int) -> None:
        if not self.enable_vector_index:
            return

        if not await self._supports_vector_index():
            if not self._warned_vector_index_unsupported:
                logger.warning(
                    "ClickHouse vector indexes require ClickHouse 25.8 or newer; "
                    "falling back to exact vector search."
                )
                self._warned_vector_index_unsupported = True
            return

        quoted_table_name = self._quote_identifier(table_name)
        quoted_index_name = self._quote_identifier(self.vector_index_name)
        try:
            await self._execute(
                f"""
                ALTER TABLE {quoted_table_name}
                ADD INDEX IF NOT EXISTS {quoted_index_name}
                vector TYPE vector_similarity('hnsw', cosineDistance, {int(vector_size)})
                GRANULARITY {int(self.vector_index_granularity)}
                """
            )
            await self._execute(
                f"ALTER TABLE {quoted_table_name} MATERIALIZE INDEX {quoted_index_name}",
                settings={"mutations_sync": 2},
            )
        except Exception as error:
            logger.warning(
                "Could not create ClickHouse vector index on %s: %s. "
                "Falling back to exact vector search.",
                table_name,
                error,
            )

    async def _supports_vector_index(self) -> bool:
        try:
            result = await self._fetch_one("SELECT version() AS version")
        except Exception as error:
            logger.warning("Could not detect ClickHouse version: %s", error)
            return False

        if not result:
            return False
        return self._version_at_least(str(result["version"]), (25, 8))

    @staticmethod
    def _version_at_least(version: str, minimum: tuple[int, int]) -> bool:
        parts = []
        for part in version.split("."):
            match = re.match(r"(\d+)", part)
            if match is None:
                break
            parts.append(int(match.group(1)))
        while len(parts) < len(minimum):
            parts.append(0)
        return tuple(parts[: len(minimum)]) >= minimum

    async def _execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        return await asyncio.to_thread(self._execute_sync, sql, params, settings)

    async def _insert(
        self, table_name: str, rows: list[tuple[Any, ...]], columns: list[str]
    ) -> None:
        await asyncio.to_thread(self._insert_sync, table_name, rows, columns)

    async def _fetch_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        rows = await self._fetch_all(sql, params)
        return rows[0] if rows else None

    async def _fetch_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._fetch_all_sync, sql, params)

    def _execute_sync(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        client = self._connect()
        try:
            return client.command(sql, parameters=params or {}, settings=settings or {})
        finally:
            self._close_client(client)

    def _insert_sync(
        self, table_name: str, rows: list[tuple[Any, ...]], columns: list[str]
    ) -> None:
        client = self._connect()
        try:
            client.insert(table_name, rows, column_names=columns)
        finally:
            self._close_client(client)

    def _fetch_all_sync(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        client = self._connect()
        try:
            result = client.query(sql, parameters=params or {})
            return list(result.named_results())
        finally:
            self._close_client(client)

    def _fetch_one_sync(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        rows = self._fetch_all_sync(sql, params)
        return rows[0] if rows else None

    def _connect(self):
        return clickhouse_connect.get_client(**self.connection_config)

    @staticmethod
    def _close_client(client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def _build_connection_config(self, url: str | None, api_key: str | None) -> dict[str, Any]:
        api_key_config = self._decode_api_key(api_key)
        url_config = self._parse_url(url)
        password = self._first_present(
            os.getenv("CLICKHOUSE_PASSWORD"),
            os.getenv("VECTOR_DB_PASSWORD"),
            api_key_config.get("password"),
            api_key_config.get("api_key"),
            url_config.get("password"),
        )

        config: dict[str, Any] = {
            "host": self._first_present(
                os.getenv("CLICKHOUSE_HOST"),
                os.getenv("VECTOR_DB_HOST"),
                url_config.get("host"),
                api_key_config.get("host"),
                "localhost",
            ),
            "port": int(
                self._first_present(
                    os.getenv("CLICKHOUSE_PORT"),
                    os.getenv("VECTOR_DB_PORT"),
                    url_config.get("port"),
                    api_key_config.get("port"),
                    8123,
                )
            ),
            "username": self._first_present(
                os.getenv("CLICKHOUSE_USERNAME"),
                os.getenv("VECTOR_DB_USERNAME"),
                url_config.get("username"),
                api_key_config.get("username"),
                "default",
            ),
            "password": password if password is not None else "",
            "database": self._first_present(
                os.getenv("CLICKHOUSE_DATABASE"),
                os.getenv("VECTOR_DB_NAME"),
                api_key_config.get("database"),
                url_config.get("database"),
                "default",
            ),
            "secure": self._to_bool(
                self._first_present(
                    os.getenv("CLICKHOUSE_SECURE"),
                    os.getenv("VECTOR_DB_SECURE"),
                    url_config.get("secure"),
                    api_key_config.get("secure"),
                    False,
                )
            ),
            "compress": self._to_bool(
                self._first_present(
                    os.getenv("CLICKHOUSE_COMPRESS"),
                    api_key_config.get("compress"),
                    True,
                )
            ),
            "client_name": "cognee-community-clickhouse-vector-adapter",
        }

        optional_mapping = {
            "verify": "CLICKHOUSE_VERIFY",
            "ca_cert": "CLICKHOUSE_CA_CERT",
            "client_cert": "CLICKHOUSE_CLIENT_CERT",
            "client_cert_key": "CLICKHOUSE_CLIENT_CERT_KEY",
            "connect_timeout": "CLICKHOUSE_CONNECT_TIMEOUT",
            "send_receive_timeout": "CLICKHOUSE_SEND_RECEIVE_TIMEOUT",
        }
        for key, env_name in optional_mapping.items():
            value = self._first_present(os.getenv(env_name), api_key_config.get(key))
            if value is None:
                continue
            if key in {"verify"}:
                config[key] = self._to_bool(value)
            elif key in {"connect_timeout", "send_receive_timeout"}:
                config[key] = int(value)
            else:
                config[key] = value

        return config

    def _parse_url(self, raw_url: str | None) -> dict[str, Any]:
        if not raw_url:
            return {}

        if "://" not in raw_url:
            if "@" in raw_url or "/" in raw_url:
                raw_url = f"http://{raw_url}"
            else:
                host, _, port = raw_url.partition(":")
                return {"host": host, "port": int(port) if port else None}

        parsed = urlparse(raw_url)
        query_params = parse_qs(parsed.query)
        database = parsed.path.lstrip("/").split("/", 1)[0] if parsed.path else None
        return {
            "host": parsed.hostname,
            "port": parsed.port,
            "username": unquote(parsed.username) if parsed.username else None,
            "password": unquote(parsed.password) if parsed.password else None,
            "database": database or None,
            "secure": parsed.scheme in {"https", "clickhouses"}
            or query_params.get("secure", [None])[0],
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

    @staticmethod
    def _try_base64_decode(value: str) -> str | None:
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

    def _sanitize_identifier(self, identifier: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", identifier).strip("_")
        return sanitized or "idx_vector"

    def _quote_identifier(self, identifier: str) -> str:
        return f"`{identifier.replace('`', '``')}`"

    def _vector_size(self) -> int:
        try:
            vector_size = int(self.embedding_engine.get_vector_size())
        except Exception:
            vector_size = int(os.getenv("EMBEDDING_DIMENSIONS", "0"))

        if vector_size <= 0:
            raise ValueError(
                "ClickHouse adapter requires a positive embedding dimension. "
                "Configure an embedding engine with get_vector_size() or set EMBEDDING_DIMENSIONS."
            )
        return vector_size

    def _validate_vector(
        self, vector: Iterable[Any], expected_size: int | None = None
    ) -> list[float]:
        if hasattr(vector, "tolist") and not isinstance(vector, (str, bytes, bytearray)):
            vector = vector.tolist()

        values = list(vector)
        if not values:
            raise ValueError("ClickHouse vectors must not be empty.")
        if expected_size is not None and len(values) != expected_size:
            raise ValueError(
                "ClickHouse vector dimension mismatch: "
                f"expected {expected_size}, got {len(values)}."
            )

        result = []
        for value in values:
            float_value = float(value)
            if not math.isfinite(float_value):
                raise ValueError("ClickHouse vectors must contain only finite numeric values.")
            result.append(float_value)
        return result

    def _normalize_vector(self, vector: list[float]) -> list[float]:
        if not self.normalize_vectors:
            return [float(value) for value in vector]

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return [float(value) for value in vector]
        return [float(value) / norm for value in vector]

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

    @staticmethod
    def _get_text_content(data_point: DataPoint) -> str:
        metadata = getattr(data_point, "metadata", None)
        if isinstance(metadata, dict):
            index_fields = metadata.get("index_fields", ["text"])
            field_name = index_fields[0] if index_fields else "text"
        else:
            field_name = "text"
        return str(getattr(data_point, field_name, ""))

    @staticmethod
    def _belongs_to_set(data_point: DataPoint) -> list[str]:
        belongs_to_set = getattr(data_point, "belongs_to_set", None) or []
        return [str(value) for value in belongs_to_set]

    @staticmethod
    def _first_present(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).lower() in {"1", "true", "yes", "on", "https"}

    @staticmethod
    def _env_flag(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        value = os.getenv(name)
        if value is None:
            return default
        return int(value)
