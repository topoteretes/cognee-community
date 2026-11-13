import aiohttp
import asyncio
import base64
from typing import List, Dict, Any, Optional

class SurrealDBConnector:
    """
    SurrealDB Multi-Model Database Connector for Cognee.
    
    Extracts tabular records, document models, and graph edge relations (in/out)
    from SurrealDB via its official HTTP /sql endpoint into Cognee memory graphs.
    """

    DEFAULT_URL = "http://localhost:8000"

    def __init__(
        self,
        namespace: str,
        database: str,
        url: str = DEFAULT_URL,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tables: Optional[List[str]] = None,
        limit_per_table: int = 100,
    ):
        """
        Initialize SurrealDBConnector.

        Args:
            namespace: SurrealDB namespace name (NS).
            database: SurrealDB database name (DB).
            url: Base URL of SurrealDB instance (default: http://localhost:8000).
            token: Optional JWT Bearer token for authentication.
            username: Optional basic auth username (e.g., 'root').
            password: Optional basic auth password (e.g., 'root').
            tables: Optional list of table names to ingest. If None, discovers tables via INFO FOR DB.
            limit_per_table: Maximum records to extract per table (default: 100).
        """
        if not namespace or not database:
            raise ValueError("SurrealDB namespace (NS) and database (DB) must be provided.")

        self.namespace = namespace
        self.database = database
        self.url = url.rstrip("/")
        self.token = token
        self.username = username
        self.password = password
        self.tables = tables or []
        self.limit_per_table = max(1, min(limit_per_table, 1000))

    def _headers(self) -> Dict[str, str]:
        headers = {
            "NS": self.namespace,
            "DB": self.database,
            "Accept": "application/json",
            "Content-Type": "text/plain",
            "User-Agent": "cognee-community-connector-surrealdb/0.1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.username and self.password:
            creds = f"{self.username}:{self.password}"
            encoded = base64.b64encode(creds.encode("utf-8")).decode("utf-8")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    async def _execute_sql(self, session: aiohttp.ClientSession, sql_statement: str) -> Optional[List[Dict[str, Any]]]:
        """Execute a SurrealQL query via the /sql endpoint."""
        endpoint = f"{self.url}/sql"
        try:
            async with session.post(endpoint, data=sql_statement, headers=self._headers()) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if isinstance(data, list) and len(data) > 0:
                    first_res = data[0]
                    if first_res.get("status") == "OK":
                        return first_res.get("result", [])
                return None
        except Exception:
            return None

    async def get_tables_info(self, session: aiohttp.ClientSession) -> List[str]:
        """Inspect database schema and list available tables via INFO FOR DB;"""
        result = await self._execute_sql(session, "INFO FOR DB;")
        if not result:
            return []

        # INFO FOR DB returns a dictionary with 'tables' key
        if isinstance(result, dict) and "tables" in result:
            return list(result["tables"].keys())
        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], dict) and "tables" in result[0]:
            return list(result[0]["tables"].keys())
        return []

    def _format_record_document(self, table: str, record: Dict[str, Any]) -> Dict[str, Any]:
        """Format a SurrealDB record and its graph edges into a clean markdown document."""
        rec_id = str(record.get("id", "unknown"))
        edge_in = record.get("in")
        edge_out = record.get("out")

        # Format key-value pairs
        fields_str = []
        for k, v in record.items():
            if k in ["id", "in", "out"]:
                continue
            fields_str.append(f"- **{k}**: {v}")

        edge_info = ""
        if edge_in or edge_out:
            edge_parts = []
            if edge_in:
                edge_parts.append(f"Incoming Edge (in): `{edge_in}`")
            if edge_out:
                edge_parts.append(f"Outgoing Edge (out): `{edge_out}`")
            edge_info = "\n" + "\n".join(edge_parts)

        clean_text = f"""# SurrealDB Record: {rec_id} (Table: {table})
Namespace: {self.namespace} | Database: {self.database}
{edge_info}

## Attributes
{chr(10).join(fields_str) if fields_str else "No attributes."}
""".strip()

        return {
            "source_id": f"surrealdb:{self.namespace}:{self.database}:{table}:{rec_id}",
            "table": table,
            "record_id": rec_id,
            "edge_in": edge_in,
            "edge_out": edge_out,
            "data": record,
            "clean_text": clean_text,
        }

    async def fetch_table_records(self, session: aiohttp.ClientSession, table: str) -> List[Dict[str, Any]]:
        """Fetch records from a given SurrealDB table with configured limit."""
        query = f"SELECT * FROM {table} LIMIT {self.limit_per_table};"
        records = await self._execute_sql(session, query)
        if not records or not isinstance(records, list):
            return []

        documents = []
        for rec in records:
            if isinstance(rec, dict):
                documents.append(self._format_record_document(table, rec))
        return documents

    async def fetch_all(self) -> List[Dict[str, Any]]:
        """Fetch all configured or discovered tables and normalize records into Cognee documents."""
        async with aiohttp.ClientSession() as session:
            target_tables = self.tables
            if not target_tables:
                discovered = await self.get_tables_info(session)
                target_tables = discovered or []

            if not target_tables:
                return []

            tasks = [self.fetch_table_records(session, t) for t in target_tables]
            results = await asyncio.gather(*tasks)

            all_docs = []
            for table_docs in results:
                all_docs.extend(table_docs)
            return all_docs
