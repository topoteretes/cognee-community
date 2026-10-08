"""DLT source for Snowflake (Key-Pair Auth, Object Comments, CHANGES Feed & Timestamp Cursor).

Integrates Snowflake data and metadata into cognee's memory graph following the dual-path
connector model:
1. snowflake_comments: Document mode (DOCUMENT_SOURCE_ATTR = "snowflake"). Renders table & column
   comments into semantic table cards for text chunking and LLM entity/graph extraction.
2. snowflake_tables: Structured relational mode. Ingests tabular rows with stable source identities.
   Prefers Snowflake's native CHANGES(INFORMATION => DEFAULT) clause for zero-stream inserts,
   updates, and deletes. Falls back to timestamp column cursors with tie-breaking and key scans.
3. snowflake_queries: Explicit opt-in read queries with declared primary keys.

Anti-corruption guardrail:
If any query, warehouse timeout, or catalog listing fails during reconciliation, deletion
reconciliation is aborted immediately without emitting tombstones to protect graph state.
"""

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("snowflake_connector")
except ImportError:
    import logging

    logger = logging.getLogger("snowflake_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

SNOWFLAKE_COMMENTS_TABLE = "snowflake_comments"
SNOWFLAKE_TABLES_TABLE = "snowflake_tables"
SNOWFLAKE_QUERIES_TABLE = "snowflake_queries"
SNOWFLAKE_SOURCE_NAME = "snowflake"

_EXTRA_HINT = (
    'The Snowflake connector requires "snowflake-connector-python" and "cryptography": '
    'pip install "cognee-community-connector-snowflake"'
)


def _sanitize_string(val: str | None) -> str:
    """Sanitize strings for stable ID generation."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", val or "").strip("_")


class SnowflakeClient:
    """Client wrapper for Snowflake queries using Key-Pair authentication."""

    def __init__(
        self,
        account: str,
        user: str,
        private_key_file: str | None = None,
        private_key_pem: str | None = None,
        private_key_passphrase: str | None = None,
        warehouse: str | None = None,
        database: str | None = None,
        schema: str | None = None,
        role: str | None = None,
    ):
        self.account = account
        self.user = user
        self.private_key_file = private_key_file
        self.private_key_pem = private_key_pem
        self.private_key_passphrase = private_key_passphrase
        self.warehouse = warehouse
        self.database = database
        self.schema = schema
        self.role = role
        self._conn = None

    def _get_private_key_der(self) -> bytes:
        """Parse RSA private key from file or PEM string using cryptography."""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        pem_data: bytes
        if self.private_key_file:
            with open(self.private_key_file, "rb") as f:
                pem_data = f.read()
        elif self.private_key_pem:
            pem_data = self.private_key_pem.encode("utf-8")
        else:
            raise ValueError("Snowflake Key-Pair auth requires private_key_file or private_key_pem")

        passphrase_bytes = (
            self.private_key_passphrase.encode("utf-8") if self.private_key_passphrase else None
        )
        p_key = serialization.load_pem_private_key(
            pem_data, password=passphrase_bytes, backend=default_backend()
        )
        return p_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def connect(self):
        """Establish connection with fail-fast warehouse queue timeouts."""
        if self._conn is not None:
            return self._conn

        try:
            import snowflake.connector
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        pkb = self._get_private_key_der()
        self._conn = snowflake.connector.connect(
            account=self.account,
            user=self.user,
            private_key=pkb,
            warehouse=self.warehouse,
            database=self.database,
            schema=self.schema,
            role=self.role,
            authenticator="SNOWFLAKE_JWT",
            session_parameters={
                "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": 45,
                "STATEMENT_TIMEOUT_IN_SECONDS": 180,
                "QUERY_TAG": "cognee-community-connector-snowflake",
            },
        )
        return self._conn

    def execute_query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        """Execute a query and return rows as dictionaries."""
        conn = self.connect()
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            columns = [col[0].upper() for col in cursor.description or []]
            rows = cursor.fetchall() or []
            return [dict(zip(columns, row, strict=False)) for row in rows]
        finally:
            cursor.close()

    def fetch_table_and_column_comments(
        self, database: str, schema: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch tables, columns, and comments from INFORMATION_SCHEMA."""
        schema_filter = f"AND t.TABLE_SCHEMA = '{schema.upper()}'" if schema else ""
        sql = f"""
        SELECT
            t.TABLE_CATALOG, t.TABLE_SCHEMA, t.TABLE_NAME, t.COMMENT AS TABLE_COMMENT,
            c.COLUMN_NAME, c.DATA_TYPE, c.COMMENT AS COLUMN_COMMENT, c.ORDINAL_POSITION
        FROM {database}.INFORMATION_SCHEMA.TABLES t
        JOIN {database}.INFORMATION_SCHEMA.COLUMNS c
          ON t.TABLE_CATALOG = c.TABLE_CATALOG
         AND t.TABLE_SCHEMA = c.TABLE_SCHEMA
         AND t.TABLE_NAME = c.TABLE_NAME
        WHERE t.TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA') {schema_filter}
        ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION
        """
        return self.execute_query(sql)

    def fetch_table_keys(
        self, database: str, schema: str, table: str, primary_key: str
    ) -> list[Any]:
        """Fetch all primary keys for deletion inventory reconciliation."""
        sql = f"SELECT {primary_key} FROM {database}.{schema}.{table}"
        rows = self.execute_query(sql)
        pk_upper = primary_key.upper()
        return [r.get(pk_upper) for r in rows if r.get(pk_upper) is not None]

    def fetch_changes(
        self,
        database: str,
        schema: str,
        table: str,
        last_sync: str,
        current_sync: str,
    ) -> list[dict[str, Any]]:
        """Fetch net change records using CHANGES clause."""
        sql = f"""
        SELECT *, METADATA$ACTION, METADATA$ISUPDATE, METADATA$ROW_ID
        FROM {database}.{schema}.{table}
        CHANGES(INFORMATION => DEFAULT)
        AT(TIMESTAMP => '{last_sync}'::TIMESTAMP_NTZ)
        END(TIMESTAMP => '{current_sync}'::TIMESTAMP_NTZ)
        """
        return self.execute_query(sql)

    def fetch_rows_since_cursor(
        self,
        database: str,
        schema: str,
        table: str,
        cursor_column: str,
        cursor_val: str | None,
        primary_key: str,
    ) -> list[dict[str, Any]]:
        """Fetch rows newer than or equal to cursor for timestamp fallback."""
        if cursor_val:
            sql = f"""
            SELECT * FROM {database}.{schema}.{table}
            WHERE {cursor_column} >= '{cursor_val}'::TIMESTAMP_NTZ
            ORDER BY {cursor_column} ASC, {primary_key} ASC
            """
        else:
            sql = f"""
            SELECT * FROM {database}.{schema}.{table}
            ORDER BY {cursor_column} ASC, {primary_key} ASC
            """
        return self.execute_query(sql)

    def fetch_all_rows(self, database: str, schema: str, table: str) -> list[dict[str, Any]]:
        """Fetch all rows from a table."""
        return self.execute_query(f"SELECT * FROM {database}.{schema}.{table}")


def _build_comment_cards(raw_rows: list[dict[str, Any]], account: str) -> list[dict[str, Any]]:
    """Group table and column metadata into formatted document cards."""
    tables_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in raw_rows:
        cat = r.get("TABLE_CATALOG", "")
        sch = r.get("TABLE_SCHEMA", "")
        tbl = r.get("TABLE_NAME", "")
        key = (cat, sch, tbl)
        if key not in tables_map:
            tables_map[key] = {
                "catalog": cat,
                "schema": sch,
                "table": tbl,
                "comment": r.get("TABLE_COMMENT") or "",
                "columns": [],
            }
        col_name = r.get("COLUMN_NAME", "")
        data_type = r.get("DATA_TYPE", "")
        col_comment = r.get("COLUMN_COMMENT") or ""
        tables_map[key]["columns"].append(
            {"name": col_name, "type": data_type, "comment": col_comment}
        )

    cards = []
    for (cat, sch, tbl), info in tables_map.items():
        doc_id = f"snowflake:{_sanitize_string(account)}:{cat}.{sch}.{tbl}:schema_card".lower()
        desc_text = info["comment"] if info["comment"] else "No table description provided."
        lines = [
            f"# Table: {cat}.{sch}.{tbl}",
            f"**Description**: {desc_text}",
            "",
            "## Columns:",
        ]
        for c in info["columns"]:
            c_desc = f": {c['comment']}" if c["comment"] else ""
            lines.append(f"- `{c['name']}` ({c['type']}){c_desc}")
        card_content = "\n".join(lines)
        cards.append(
            {
                "id": doc_id,
                "title": f"Snowflake Table: {cat}.{sch}.{tbl}",
                "content": card_content,
                "url": f"snowflake://{account}/{cat}/{sch}/{tbl}",
            }
        )
    return cards


def snowflake_source(
    account: str | None = None,
    user: str | None = None,
    private_key_file: str | None = None,
    private_key_pem: str | None = None,
    private_key_passphrase: str | None = None,
    warehouse: str | None = None,
    database: str | None = None,
    schema: str | None = None,
    role: str | None = None,
    tables: list[dict[str, Any]] | None = None,
    queries: list[dict[str, Any]] | None = None,
    include_comments: bool = True,
    client: Any = None,
):
    """Create a dlt source for Snowflake data, comments, and explicit queries.

    Args:
        account: Snowflake account locator / identifier.
        user: Snowflake username.
        private_key_file: Path to unencrypted or encrypted PKCS#8 RSA private key.
        private_key_pem: String PEM content of RSA private key.
        private_key_passphrase: Password for encrypted private key.
        warehouse: Snowflake warehouse name.
        database: Snowflake database name.
        schema: Optional default schema name.
        role: Optional Snowflake role.
        tables: List of table configs:
            [{"database": "...", "schema": "...", "table": "...", "primary_key": "ID",
              "use_changes": True, "cursor_column": "UPDATED_AT"}]
        queries: List of explicit query configs:
            [{"name": "...", "sql": "...", "primary_key": "ID", "cursor_column": "UPDATED_AT"}]
        include_comments: If True, generates semantic document cards from schema comments.
        client: Optional pre-built SnowflakeClient (test injection).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_account = account or os.environ.get("SNOWFLAKE_ACCOUNT")
    resolved_user = user or os.environ.get("SNOWFLAKE_USER")
    resolved_key_file = private_key_file or os.environ.get("SNOWFLAKE_PRIVATE_KEY_FILE")
    resolved_key_pem = private_key_pem or os.environ.get("SNOWFLAKE_PRIVATE_KEY_PEM")
    resolved_passphrase = private_key_passphrase or os.environ.get(
        "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"
    )
    resolved_warehouse = warehouse or os.environ.get("SNOWFLAKE_WAREHOUSE")
    resolved_database = database or os.environ.get("SNOWFLAKE_DATABASE")
    resolved_schema = schema or os.environ.get("SNOWFLAKE_SCHEMA")
    resolved_role = role or os.environ.get("SNOWFLAKE_ROLE")

    if client is None:
        if not resolved_account or not resolved_user:
            raise ValueError(
                "Snowflake authentication requires account and user. "
                "Pass parameters or set SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER."
            )
        if not resolved_key_file and not resolved_key_pem:
            raise ValueError(
                "Snowflake Key-Pair authentication requires private_key_file or private_key_pem."
            )
        client = SnowflakeClient(
            account=resolved_account,
            user=resolved_user,
            private_key_file=resolved_key_file,
            private_key_pem=resolved_key_pem,
            private_key_passphrase=resolved_passphrase,
            warehouse=resolved_warehouse,
            database=resolved_database,
            schema=resolved_schema,
            role=resolved_role,
        )

    account_tag = _sanitize_string(resolved_account or getattr(client, "account", "snowflake"))

    @dlt.resource(name=SNOWFLAKE_COMMENTS_TABLE, primary_key="id", write_disposition="replace")
    def snowflake_comments() -> Iterator[dict[str, Any]]:
        """Yield table and column comments as document cards."""
        db_target = resolved_database or getattr(client, "database", None)
        if not db_target:
            logger.info("Snowflake: No database specified; skipping comments extraction.")
            return

        try:
            raw_meta = client.fetch_table_and_column_comments(
                database=db_target, schema=resolved_schema
            )
            cards = _build_comment_cards(raw_meta, account_tag)
            yield from cards
            logger.info("Snowflake: synced %d comment card(s).", len(cards))
        except Exception as exc:
            logger.error("Snowflake: failed to fetch comments metadata: %s", exc)
            raise

    @dlt.resource(name=SNOWFLAKE_TABLES_TABLE, primary_key="id", write_disposition="merge")
    def snowflake_tables() -> Iterator[dict[str, Any]]:
        """Yield structured rows with CHANGES feed and timestamp fallback."""
        if not tables:
            return

        state = dlt.current.resource_state()
        tables_state = state.setdefault("tables", {})

        for tbl_cfg in tables:
            db = tbl_cfg.get("database") or resolved_database
            sch = tbl_cfg.get("schema") or resolved_schema or "PUBLIC"
            tbl = tbl_cfg.get("table")
            pk = tbl_cfg.get("primary_key", "ID")
            use_changes = tbl_cfg.get("use_changes", True)
            cursor_col = tbl_cfg.get("cursor_column")

            if not db or not tbl:
                logger.warning("Snowflake table config missing database or table name: %s", tbl_cfg)
                continue

            tbl_key = f"{db}.{sch}.{tbl}".lower()
            cur_state = tables_state.setdefault(tbl_key, {})
            last_checkpoint = cur_state.get("last_checkpoint")
            known_ids = set(cur_state.get("known_ids", []))
            sync_now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

            # --- Primary Mode: CHANGES Clause ---
            changes_succeeded = False
            if use_changes and last_checkpoint:
                try:
                    logger.info(
                        "Snowflake: querying CHANGES for %s since %s", tbl_key, last_checkpoint
                    )
                    rows = client.fetch_changes(
                        database=db,
                        schema=sch,
                        table=tbl,
                        last_sync=last_checkpoint,
                        current_sync=sync_now,
                    )
                    pk_upper = pk.upper()
                    for r in rows:
                        action = str(r.get("METADATA$ACTION", "")).upper()
                        is_update = bool(r.get("METADATA$ISUPDATE", False))
                        pk_val = r.get(pk_upper) or r.get(pk)
                        if pk_val is None:
                            continue

                        row_id = f"snowflake:{account_tag}:{db}.{sch}.{tbl}:{pk_val}".lower()
                        if action == "DELETE" and not is_update:
                            known_ids.discard(str(pk_val))
                            yield {"id": row_id, "_deleted": True}
                        else:
                            known_ids.add(str(pk_val))
                            cleaned = {k: v for k, v in r.items() if not k.startswith("METADATA$")}
                            cleaned["id"] = row_id
                            cleaned["_deleted"] = False
                            yield cleaned

                    cur_state["last_checkpoint"] = sync_now
                    cur_state["known_ids"] = sorted(known_ids)
                    changes_succeeded = True
                except Exception as exc:
                    logger.warning(
                        "Snowflake: CHANGES query failed for %s (retention/tracking): %s. "
                        "Falling back to reconciliation.",
                        tbl_key,
                        exc,
                    )

            if changes_succeeded:
                continue

            # --- Fallback Mode: Timestamp Cursor or Full Reconcile ---
            if cursor_col:
                last_cursor = cur_state.get("last_cursor")
                keys_at_cursor = set(cur_state.get("keys_seen_at_cursor", []))
                try:
                    rows = client.fetch_rows_since_cursor(
                        database=db,
                        schema=sch,
                        table=tbl,
                        cursor_column=cursor_col,
                        cursor_val=last_cursor,
                        primary_key=pk,
                    )
                    max_seen_cursor = last_cursor
                    new_keys_at_max: set[str] = set()
                    pk_upper = pk.upper()
                    cursor_upper = cursor_col.upper()

                    for r in rows:
                        pk_val = str(r.get(pk_upper) or r.get(pk) or "")
                        if not pk_val:
                            continue
                        cur_val = str(r.get(cursor_upper) or r.get(cursor_col) or "")
                        if cur_val == last_cursor and pk_val in keys_at_cursor:
                            continue

                        row_id = f"snowflake:{account_tag}:{db}.{sch}.{tbl}:{pk_val}".lower()
                        known_ids.add(pk_val)
                        row_dict = dict(r)
                        row_dict["id"] = row_id
                        row_dict["_deleted"] = False
                        yield row_dict

                        if max_seen_cursor is None or cur_val > max_seen_cursor:
                            max_seen_cursor = cur_val
                            new_keys_at_max = {pk_val}
                        elif cur_val == max_seen_cursor:
                            new_keys_at_max.add(pk_val)

                    # Update cursor state
                    cur_state["last_cursor"] = max_seen_cursor
                    cur_state["keys_seen_at_cursor"] = sorted(new_keys_at_max)
                    cur_state["last_checkpoint"] = sync_now

                    # Key-scan deletion reconciliation
                    try:
                        current_keys = {
                            str(k)
                            for k in client.fetch_table_keys(
                                database=db, schema=sch, table=tbl, primary_key=pk
                            )
                        }
                        deleted_keys = known_ids - current_keys
                        for d_key in deleted_keys:
                            del_id = f"snowflake:{account_tag}:{db}.{sch}.{tbl}:{d_key}".lower()
                            yield {"id": del_id, "_deleted": True}
                        cur_state["known_ids"] = sorted(current_keys)
                    except Exception as scan_exc:
                        logger.error(
                            "Snowflake: key inventory scan failed for %s; aborting deletion: %s",
                            tbl_key,
                            scan_exc,
                        )
                except Exception as exc:
                    logger.error("Snowflake: timestamp fetch failed for %s: %s", tbl_key, exc)
                    raise
            else:
                # Full snapshot reconcile
                try:
                    all_rows = client.fetch_all_rows(database=db, schema=sch, table=tbl)
                    current_keys = set()
                    pk_upper = pk.upper()
                    for r in all_rows:
                        pk_val = str(r.get(pk_upper) or r.get(pk) or "")
                        if not pk_val:
                            continue
                        current_keys.add(pk_val)
                        row_id = f"snowflake:{account_tag}:{db}.{sch}.{tbl}:{pk_val}".lower()
                        row_dict = dict(r)
                        row_dict["id"] = row_id
                        row_dict["_deleted"] = False
                        yield row_dict

                    deleted_keys = known_ids - current_keys
                    for d_key in deleted_keys:
                        del_id = f"snowflake:{account_tag}:{db}.{sch}.{tbl}:{d_key}".lower()
                        yield {"id": del_id, "_deleted": True}

                    cur_state["known_ids"] = sorted(current_keys)
                    cur_state["last_checkpoint"] = sync_now
                except Exception as exc:
                    logger.error("Snowflake: full reconcile failed for %s: %s", tbl_key, exc)
                    raise

    @dlt.resource(name=SNOWFLAKE_QUERIES_TABLE, primary_key="id", write_disposition="merge")
    def snowflake_queries() -> Iterator[dict[str, Any]]:
        """Yield explicitly declared SQL query results."""
        if not queries:
            return

        for q_cfg in queries:
            q_name = _sanitize_string(q_cfg.get("name") or "custom_query")
            sql = q_cfg.get("sql")
            pk = q_cfg.get("primary_key", "ID")

            if not sql:
                logger.warning("Snowflake query config missing sql statement: %s", q_cfg)
                continue

            try:
                rows = client.execute_query(sql)
                pk_upper = pk.upper()
                for r in rows:
                    pk_val = r.get(pk_upper) or r.get(pk)
                    row_id = f"snowflake:{account_tag}:query:{q_name}:{pk_val}".lower()
                    row_dict = dict(r)
                    row_dict["id"] = row_id
                    row_dict["_deleted"] = False
                    yield row_dict
            except Exception as exc:
                logger.error("Snowflake: failed to execute query '%s': %s", q_name, exc)
                raise

    @dlt.source(name=SNOWFLAKE_SOURCE_NAME)
    def _snowflake():
        active = []
        if include_comments:
            active.append(snowflake_comments)
        if tables:
            active.append(snowflake_tables)
        if queries:
            active.append(snowflake_queries)
        return active

    source = _snowflake()
    # Route snowflake_comments specifically through document mode
    if include_comments:
        setattr(snowflake_comments, DOCUMENT_SOURCE_ATTR, "snowflake")
        if "snowflake_comments" in source.resources:
            setattr(source.resources["snowflake_comments"], DOCUMENT_SOURCE_ATTR, "snowflake")

    return source
