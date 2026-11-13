import aiohttp
import asyncio
from typing import List, Dict, Any, Optional

class MotherDuckConnector:
    """
    MotherDuck Serverless DuckDB Connector for Cognee.
    
    Ingests cloud database catalogs, shared datasets, and recent query execution
    telemetry from MotherDuck REST API into Cognee memory graphs.
    """

    DEFAULT_BASE_URL = "https://api.motherduck.com/v1"

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        fetch_queries: bool = True,
        query_limit: int = 50,
        fetch_shares: bool = True,
    ):
        """
        Initialize MotherDuckConnector.

        Args:
            token: MotherDuck service or user access token.
            base_url: Base URL for MotherDuck REST API.
            fetch_queries: Whether to fetch query execution telemetry.
            query_limit: Maximum number of recent queries to ingest (guards against graph bloat).
            fetch_shares: Whether to fetch database share catalogs.
        """
        if not token:
            raise ValueError("MotherDuck token must be provided.")

        self.token = token
        self.base_url = base_url.rstrip("/")
        self.fetch_queries = fetch_queries
        self.query_limit = max(1, min(query_limit, 200))
        self.fetch_shares = fetch_shares

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-motherduck/0.1.0",
        }

    async def fetch_databases(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch list of databases and storage metrics."""
        url = f"{self.base_url}/databases"
        try:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("databases", data if isinstance(data, list) else [])
                databases = []
                for db in items:
                    name = db.get("name", "unnamed_db")
                    size_bytes = db.get("size_bytes") or db.get("size", 0)
                    owner = db.get("owner", "unknown")
                    updated_at = db.get("updated_at") or db.get("last_modified", "unknown")
                    is_shared = db.get("is_shared", False)
                    clean_text = f"""# MotherDuck Database: {name}
Owner: {owner}
Size (Bytes): {size_bytes}
Last Modified: {updated_at}
Shared: {'Yes' if is_shared else 'No'}
Description: Serverless DuckDB cloud database instance.
""".strip()
                    databases.append({
                        "source_id": f"motherduck:database:{name}",
                        "name": name,
                        "owner": owner,
                        "size_bytes": size_bytes,
                        "updated_at": updated_at,
                        "is_shared": is_shared,
                        "clean_text": clean_text,
                    })
                return databases
        except Exception:
            return []

    async def fetch_shares_list(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch database sharing catalogs and external permissions."""
        if not self.fetch_shares:
            return []

        url = f"{self.base_url}/shares"
        try:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("shares", data if isinstance(data, list) else [])
                shares = []
                for s in items:
                    share_name = s.get("name") or s.get("share_name", "unnamed_share")
                    db_name = s.get("database_name") or s.get("database", "unknown")
                    access_level = s.get("access_level") or s.get("permission", "read_only")
                    created_at = s.get("created_at", "unknown")
                    clean_text = f"""# MotherDuck Share: {share_name}
Target Database: {db_name}
Access Level: {access_level}
Created At: {created_at}
Description: DuckDB cloud share catalog link.
""".strip()
                    shares.append({
                        "source_id": f"motherduck:share:{share_name}",
                        "share_name": share_name,
                        "database_name": db_name,
                        "access_level": access_level,
                        "created_at": created_at,
                        "clean_text": clean_text,
                    })
                return shares
        except Exception:
            return []

    async def fetch_recent_queries(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch bounded recent query telemetry to analyze system execution load."""
        if not self.fetch_queries:
            return []

        url = f"{self.base_url}/queries"
        params = {"limit": str(self.query_limit)}
        try:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("queries", data if isinstance(data, list) else [])
                queries = []
                for q in items[:self.query_limit]:
                    q_id = q.get("id") or q.get("query_id", "unknown_query")
                    sql = (q.get("query_text") or q.get("sql") or "").strip()
                    duration_ms = q.get("duration_ms") or q.get("duration", 0)
                    status = q.get("status", "SUCCESS")
                    ts = q.get("timestamp") or q.get("started_at", "unknown")
                    rows_read = q.get("rows_read") or q.get("rows_scanned", 0)

                    clean_text = f"""# MotherDuck Query Telemetry: {q_id}
Status: {status}
Executed At: {ts}
Duration (ms): {duration_ms}
Rows Scanned: {rows_read}

SQL Statement:
```sql
{sql}
```
""".strip()
                    queries.append({
                        "source_id": f"motherduck:query:{q_id}",
                        "query_id": q_id,
                        "sql": sql,
                        "duration_ms": duration_ms,
                        "status": status,
                        "timestamp": ts,
                        "rows_read": rows_read,
                        "clean_text": clean_text,
                    })
                return queries
        except Exception:
            return []

    async def fetch_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch all configured databases, shares, and recent queries."""
        async with aiohttp.ClientSession() as session:
            db_task = self.fetch_databases(session)
            shares_task = self.fetch_shares_list(session)
            queries_task = self.fetch_recent_queries(session)

            databases, shares, queries = await asyncio.gather(
                db_task, shares_task, queries_task
            )

            return {
                "databases": databases,
                "shares": shares,
                "queries": queries,
            }
