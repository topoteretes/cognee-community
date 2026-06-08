import asyncio
import json
import os
from typing import Any

import psycopg2
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.shared.logging_utils import get_logger
from psycopg2.extras import RealDictCursor

logger = get_logger("OpenGaussAdapter")


class OpenGaussAdapter(VectorDBInterface):
    """openGauss DataVec vector database adapter — HNSW/IVFFLAT/IVFPQ index support."""

    name = "openGauss"

    def __init__(
        self,
        url: str,
        api_key: str | None,
        embedding_engine: EmbeddingEngine,
        database_name: str = "cognee",
        schema_name: str = "",
        index_type: str = "",
        distance_strategy: str = "",
        embedding_dimension: int = 0,
    ):
        """Initialize adapter with connection URL, embedding engine, and index config."""
        self.url = url
        self.database_name = database_name
        self.schema_name = schema_name or os.getenv("OPENGAUSS_SCHEMA_NAME", "cognee")
        self.embedding_engine = embedding_engine
        self.index_type = (index_type or os.getenv("OPENGAUSS_INDEX_TYPE", "HNSW")).upper()
        self.distance_strategy = (
            distance_strategy or os.getenv("OPENGAUSS_DISTANCE_STRATEGY", "COSINE")
        ).upper()
        self.embedding_dimension = embedding_dimension or int(
            os.getenv("EMBEDDING_DIMENSIONS", "1536")
        )
        self.create_index = os.getenv("OPENGAUSS_CREATE_INDEX", "false").lower() in (
            "true",
            "1",
            "yes",
        )

        self._validate_config()

        self.VECTOR_DB_LOCK = asyncio.Lock()
        self._connection = None

    def _validate_config(self):
        """Warn on unknown index type, raise on invalid distance strategy."""
        valid_indexes = ["HNSW", "IVFFLAT", "IVFPQ", "HNSW-PQ"]
        if self.index_type not in valid_indexes:
            logger.warning(
                f"Index type '{self.index_type}' may not be supported. "
                f"Recommended: {', '.join(valid_indexes)}"
            )

        valid_distances = ["COSINE", "EUCLIDEAN", "MANHATTAN", "INNER_PROD"]
        if self.distance_strategy not in valid_distances:
            raise ValueError(
                f"Invalid distance strategy: {self.distance_strategy}. "
                f"Must be one of: {', '.join(valid_distances)}"
            )

    def _get_connection(self):
        """Return a live psycopg2 connection, reconnecting on failure."""
        import time

        max_retries = 3
        for attempt in range(max_retries):
            if self._connection is None or self._connection.closed != 0:
                try:
                    self._connection = psycopg2.connect(self.url)
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    raise ConnectionError(f"openGauss connection failed: {e}") from e
                assert self._connection is not None
                self._connection.autocommit = False
                self._ensure_schema(self._connection, self.schema_name)

            assert self._connection is not None
            try:
                cur = self._connection.cursor()
                cur.execute("SELECT 1")
                cur.close()
                self._connection.rollback()
                return self._connection
            except Exception:
                self._connection = None
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                raise

    @staticmethod
    def _ensure_schema(conn, schema_name: str) -> None:
        """Create the target schema if missing and set it as search_path."""
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (schema_name,),
            )
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA {schema_name}")
            cur.execute(f"SET search_path TO {schema_name}")
            conn.commit()
        finally:
            cur.close()

    def _get_cursor(self):
        """Get database cursor with RealDictCursor factory"""
        conn = self._get_connection()
        return conn.cursor(cursor_factory=RealDictCursor)

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        """Embed a list of text strings into vector representations."""
        try:
            return await self.embedding_engine.embed_text(data)  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Text embedding failed: {e}")
            raise

    async def has_collection(self, collection_name: str) -> bool:
        """Check whether a table exists in the configured schema."""
        cursor = self._get_cursor()
        conn = self._get_connection()
        try:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE LOWER(table_name) = LOWER(%s)
                      AND table_schema = %s
                );
                """,
                (collection_name, self.schema_name),
            )
            return cursor.fetchone()["exists"]  # type: ignore[no-any-return]
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to check table existence ({collection_name}): {e}")
            return False
        finally:
            cursor.close()

    async def create_collection(
        self,
        collection_name: str,
        payload_schema: object | None = None,
    ) -> None:
        """Create a vector table under the VECTOR_DB_LOCK."""
        async with self.VECTOR_DB_LOCK:
            await self._create_collection(collection_name)

    async def _create_collection(self, collection_name: str) -> None:
        """Unlocked internal helper — caller must hold VECTOR_DB_LOCK."""
        cursor = self._get_cursor()
        conn = self._get_connection()

        try:
            if await self.has_collection(collection_name):
                return

            vector_dim = self.embedding_dimension
            if hasattr(self.embedding_engine, "get_vector_size"):
                try:
                    vector_dim = self.embedding_engine.get_vector_size()
                    self.embedding_dimension = vector_dim
                except Exception as e:
                    logger.warning(
                        f"Could not get vector dimension from embedding engine: {e}. "
                        f"Using default: {vector_dim}"
                    )

            cursor.execute(
                f"""
                CREATE TABLE {collection_name} (
                    id VARCHAR(36) PRIMARY KEY,
                    vector VECTOR({vector_dim}),
                    text TEXT,
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()
            logger.info(
                f"Created table {collection_name} (dim={vector_dim}, index={self.index_type})"
            )

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to create table ({collection_name}): {e}")
            raise
        finally:
            cursor.close()

    @staticmethod
    def _get_text_content(data_point: DataPoint) -> str:
        """Extract the primary text field from a DataPoint for embedding."""
        metadata = getattr(data_point, "metadata", None)
        if isinstance(metadata, dict):
            index_fields = metadata.get("index_fields", ["text"])
            field_name = index_fields[0] if index_fields else "text"
        else:
            field_name = "text"
        return getattr(data_point, field_name, "")

    async def create_data_points(
        self,
        collection_name: str,
        data_points: list[DataPoint],
    ) -> None:
        """Embed and batch-insert data points with DELETE+INSERT upsert."""
        if not data_points:
            return

        texts = [DataPoint.get_embeddable_data(dp) for dp in data_points]
        vectors = await self.embed_data(texts)

        async with self.VECTOR_DB_LOCK:
            cursor = self._get_cursor()
            conn = self._get_connection()

            try:
                insert_data = []
                for data_point, vector in zip(data_points, vectors, strict=False):
                    insert_data.append(
                        (
                            str(data_point.id),
                            str(vector).replace(" ", ""),
                            self._get_text_content(data_point),
                            json.dumps(getattr(data_point, "metadata", {})),
                        )
                    )

                ids = [row[0] for row in insert_data]
                cursor.execute(
                    f"DELETE FROM {collection_name} WHERE id = ANY(%s)",
                    (ids,),
                )

                cursor.executemany(
                    f"INSERT INTO {collection_name} (id, vector, text, metadata) "
                    "VALUES (%s, %s::vector, %s, %s::jsonb);",
                    insert_data,
                )
                conn.commit()

                logger.info(f"Inserted {len(data_points)} rows into table: {collection_name}")

            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to insert data points ({collection_name}): {e}")
                raise
            finally:
                cursor.close()

    async def create_vector_index(
        self,
        index_name: str,
        index_property_name: str,
    ) -> None:
        """Ensure the table exists, optionally create a vector index."""
        collection_name = f"{index_name}_{index_property_name}"
        async with self.VECTOR_DB_LOCK:
            await self._create_collection(collection_name)

            if not self.create_index:
                return

            cursor = self._get_cursor()
            conn = self._get_connection()
            try:
                vector_index_name = f"idx_{collection_name}_{self.index_type.lower()}"

                distance_ops = {
                    "COSINE": "vector_cosine_ops",
                    "EUCLIDEAN": "vector_l2_ops",
                    "MANHATTAN": "vector_l1_ops",
                    "INNER_PROD": "vector_inner_product_ops",
                }
                op_class = distance_ops[self.distance_strategy]

                if self.index_type == "HNSW":
                    sql = (
                        f"CREATE INDEX IF NOT EXISTS {vector_index_name} ON {collection_name} "
                        f"USING hnsw (vector {op_class}) WITH (m=16, ef_construction=200);"
                    )
                elif self.index_type == "IVFFLAT":
                    sql = (
                        f"CREATE INDEX IF NOT EXISTS {vector_index_name} ON {collection_name} "
                        f"USING ivfflat (vector {op_class}) WITH (lists=100);"
                    )
                elif self.index_type == "IVFPQ":
                    sql = (
                        f"CREATE INDEX IF NOT EXISTS {vector_index_name} ON {collection_name} "
                        f"USING ivfflat (vector {op_class}) "
                        f"WITH (lists=100, enable_pq=on, pq_m=2000);"
                    )
                elif self.index_type == "HNSW-PQ":
                    sql = (
                        f"CREATE INDEX IF NOT EXISTS {vector_index_name} ON {collection_name} "
                        f"USING hnsw (vector {op_class}) "
                        f"WITH (m=16, ef_construction=200, enable_pq=on, pq_m=2000);"
                    )
                else:
                    raise ValueError(f"Unsupported index type: {self.index_type}")

                cursor.execute(sql)
                conn.commit()
                logger.info(f"Created vector index {vector_index_name} (type={self.index_type})")

            except Exception as e:
                conn.rollback()
                logger.warning(f"Skipping index on {collection_name}: {e}")
                self._connection = None
            finally:
                cursor.close()

    async def index_data_points(
        self,
        index_name: str,
        index_property_name: str,
        data_points: list[DataPoint],
    ) -> None:
        """Write data points into the {index_name}_{index_property_name} table."""
        collection_name = f"{index_name}_{index_property_name}"
        await self.create_data_points(collection_name, data_points)

    async def retrieve(
        self,
        collection_name: str,
        data_point_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Look up data points by their IDs."""
        if not data_point_ids:
            return []

        cursor = self._get_cursor()
        try:
            cursor.execute(
                f"SELECT id, text, metadata, vector FROM {collection_name} WHERE id = ANY(%s);",
                (data_point_ids,),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to retrieve data points ({collection_name}): {e}")
            raise
        finally:
            cursor.close()

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
        node_name_filter_operator: str = "OR",
        **kwargs: object,
    ) -> list[dict[str, object]]:
        """Vector similarity search. Provide query_text xor query_vector."""
        if not await self.has_collection(collection_name):
            return []

        if query_vector is not None:
            search_vector = query_vector
        elif query_text is not None:
            query_vectors = await self.embed_data([query_text])
            search_vector = query_vectors[0]
        else:
            raise MissingQueryParameterError(
                "Either query_text or query_vector parameter must be provided"
            )

        cursor = self._get_cursor()
        try:
            distance_operators = {
                "COSINE": "<=>",
                "EUCLIDEAN": "<->",
                "MANHATTAN": "<|>",
                "INNER_PROD": "#>",
            }
            op = distance_operators[self.distance_strategy]

            select_fields = ["id"]
            if include_payload or with_vector:
                select_fields.extend(["text", "metadata"])
            if with_vector:
                select_fields.append("vector")

            vector_str = str(search_vector).replace(" ", "")

            if self.distance_strategy == "COSINE":
                score_expr = "(1 - (vector <=> %s::vector))"
            elif self.distance_strategy == "INNER_PROD":
                score_expr = "(-(vector #> %s::vector))"
            else:
                score_expr = f"(1 / (1 + vector {op} %s::vector))"

            search_sql = f"""
                SELECT {", ".join(select_fields)},
                       {score_expr} AS score
                FROM {collection_name}
                ORDER BY vector {op} %s::vector
                LIMIT %s;
            """

            cursor.execute(search_sql, (vector_str, vector_str, limit))

            scored_results = []
            for row in cursor:
                row_dict = dict(row)
                scored_results.append(
                    ScoredResult(
                        id=row_dict["id"],
                        score=row_dict.get("score", 0.0),
                        payload={"text": row_dict.get("text")},
                        metadata=row_dict.get("metadata", {}),
                        vector=row_dict.get("vector") if with_vector else None,
                    )
                )

            logger.info(f"Search in {collection_name} returned {len(scored_results)} results")
            return scored_results

        except Exception as e:
            logger.error(f"Vector search failed ({collection_name}): {e}")
            raise
        finally:
            cursor.close()

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int = 15,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
    ) -> list[list[ScoredResult]]:
        """Search with multiple query texts, returning results per query."""
        if not query_texts:
            return []

        all_query_vectors = await self.embed_data(query_texts)
        batch_results = []
        for query_vector in all_query_vectors:
            results = await self.search(
                collection_name=collection_name,
                query_vector=query_vector,
                limit=limit,
                with_vector=with_vectors,
                include_payload=include_payload,
                node_name=node_name,
            )
            batch_results.append(results)

        return batch_results

    async def delete_data_points(
        self,
        collection_name: str,
        data_point_ids: list[str],
    ) -> dict[str, int]:
        """Delete data points by ID, returning the count removed."""
        if not data_point_ids:
            return {"deleted_count": 0}

        async with self.VECTOR_DB_LOCK:
            cursor = self._get_cursor()
            conn = self._get_connection()
            try:
                cursor.execute(
                    f"DELETE FROM {collection_name} WHERE id = ANY(%s);",
                    (data_point_ids,),
                )
                deleted_count = cursor.rowcount
                conn.commit()
                logger.info(f"Deleted {deleted_count} rows from table: {collection_name}")
                return {"deleted_count": deleted_count}
            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to delete rows ({collection_name}): {e}")
                raise
            finally:
                cursor.close()

    async def prune(self) -> None:
        """Drop all vector tables in the configured schema."""
        cursor = self._get_cursor()
        conn = self._get_connection()
        try:
            cursor.execute(
                """
                SELECT DISTINCT c.table_name
                FROM information_schema.columns c
                WHERE c.column_name = 'vector'
                  AND c.data_type = 'vector'
                  AND c.table_schema = %s;
                """,
                (self.schema_name,),
            )
            tables_to_delete = [row["table_name"] for row in cursor.fetchall()]

            for table_name in tables_to_delete:
                cursor.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE;")
            conn.commit()
            logger.info(f"Pruned {len(tables_to_delete)} tables")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to prune: {e}")
            raise
        finally:
            cursor.close()

    def get_collection_names(self) -> list[str]:
        """Return table names from the configured schema."""
        cursor = self._get_cursor()
        try:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s;",
                (self.schema_name,),
            )
            return [row["table_name"] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get table names: {e}")
            return []
        finally:
            cursor.close()

    async def health_check(self) -> dict[str, Any]:
        """Report connection health, version, and collection list."""
        health_info: dict[str, Any] = {
            "status": "unknown",
            "adapter_name": self.name,
            "database_name": self.database_name,
            "schema_name": self.schema_name,
            "index_type": self.index_type,
            "distance_strategy": self.distance_strategy,
            "embedding_dimension": self.embedding_dimension,
        }

        try:
            cursor = self._get_cursor()
            cursor.execute("SELECT version();")
            health_info["database_version"] = cursor.fetchone()["version"]
            cursor.close()
            health_info["status"] = "healthy"
            health_info["collections"] = self.get_collection_names()
        except Exception as e:
            health_info["status"] = "unhealthy"
            health_info["error"] = str(e)

        return health_info

    async def close(self):
        """Close the underlying database connection."""
        if self._connection and self._connection.closed == 0:
            self._connection.close()
            self._connection = None
