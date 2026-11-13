import aiohttp
import asyncio
from typing import List, Dict, Any, Optional

class MeilisearchConnector:
    """
    Meilisearch Search Engine & Document Index Connector for Cognee.
    
    Extracts searchable documents, facets, and structured attributes from Meilisearch
    indexes via official REST endpoints into Cognee memory graphs.
    """

    DEFAULT_HOST = "http://localhost:7700"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        api_key: Optional[str] = None,
        indexes: Optional[List[str]] = None,
        limit_per_index: int = 50,
        fields: Optional[List[str]] = None,
    ):
        """
        Initialize MeilisearchConnector.

        Args:
            host: Base URL of Meilisearch instance (default: http://localhost:7700).
            api_key: Meilisearch Master or API Key.
            indexes: Optional list of index UIDs. If None, discovers all accessible indexes.
            limit_per_index: Maximum documents to extract per index (default: 50).
            fields: Optional allowlist of fields to extract (default: all fields).
        """
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.indexes = indexes or []
        self.limit_per_index = max(1, min(limit_per_index, 500))
        self.fields = fields

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-meilisearch/0.1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def get_indexes(self, session: aiohttp.ClientSession) -> List[str]:
        """Fetch list of all available index UIDs."""
        url = f"{self.host}/indexes"
        try:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                results = data.get("results", data if isinstance(data, list) else [])
                uids = []
                for item in results:
                    uid = item.get("uid")
                    if uid:
                        uids.append(uid)
                return uids
        except Exception:
            return []

    def _format_document(self, index_uid: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        """Format a Meilisearch document into a clean, searchable markdown document."""
        doc_id = str(doc.get("id") or doc.get("uid") or doc.get("_id") or "unidentified")
        
        # Format fields
        field_lines = []
        for k, v in doc.items():
            if self.fields and k not in self.fields:
                continue
            field_lines.append(f"- **{k}**: {v}")

        clean_text = f"""# Meilisearch Document: {doc_id} (Index: {index_uid})
Index UID: {index_uid}
Document ID: {doc_id}

## Attributes
{chr(10).join(field_lines) if field_lines else "No attributes."}
""".strip()

        return {
            "source_id": f"meilisearch:{index_uid}:{doc_id}",
            "index_uid": index_uid,
            "doc_id": doc_id,
            "data": doc,
            "clean_text": clean_text,
        }

    async def fetch_index_documents(self, session: aiohttp.ClientSession, index_uid: str) -> List[Dict[str, Any]]:
        """Fetch paginated documents from a given index."""
        url = f"{self.host}/indexes/{index_uid}/documents"
        params = {"limit": str(self.limit_per_index)}
        if self.fields:
            params["fields"] = ",".join(self.fields)

        try:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                results = data.get("results", data if isinstance(data, list) else [])
                documents = []
                for doc in results:
                    if isinstance(doc, dict):
                        documents.append(self._format_document(index_uid, doc))
                return documents
        except Exception:
            return []

    async def fetch_all(self) -> List[Dict[str, Any]]:
        """Fetch all documents across configured or auto-discovered indexes."""
        async with aiohttp.ClientSession() as session:
            target_indexes = self.indexes
            if not target_indexes:
                target_indexes = await self.get_indexes(session)

            if not target_indexes:
                return []

            tasks = [self.fetch_index_documents(session, idx) for idx in target_indexes]
            results = await asyncio.gather(*tasks)

            all_docs = []
            for idx_docs in results:
                all_docs.extend(idx_docs)
            return all_docs
