import aiohttp
import asyncio
from typing import List, Dict, Any, Optional

class OpenMLConnector:
    """
    OpenML Dataset Metadata Connector for Cognee.
    
    Extracts tabular benchmark metadata, qualities, target attributes,
    citations, and licensing from the official OpenML REST API without downloading
    heavy data files or running experiments.
    """

    DEFAULT_BASE_URL = "https://www.openml.org/api/v1/json"

    def __init__(
        self,
        dataset_ids: Optional[List[int]] = None,
        base_url: str = DEFAULT_BASE_URL,
        include_qualities: bool = True,
        api_key: Optional[str] = None,
    ):
        """
        Initialize OpenMLConnector.

        Args:
            dataset_ids: List of numeric OpenML dataset IDs to fetch.
            base_url: Base URL for OpenML REST API.
            include_qualities: Whether to fetch dataset qualities (features, instances, classes).
            api_key: Optional API key for private/restricted datasets (OpenML supports anonymous access for public).
        """
        self.dataset_ids = dataset_ids or []
        self.base_url = base_url.rstrip("/")
        self.include_qualities = include_qualities
        self.api_key = api_key

    async def _fetch_dataset_metadata(self, session: aiohttp.ClientSession, dataset_id: int) -> Optional[Dict[str, Any]]:
        """Fetch primary dataset description by ID."""
        url = f"{self.base_url}/data/{dataset_id}"
        params = {}
        if self.api_key:
            params["api_key"] = self.api_key

        try:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                return data.get("data_set_description")
        except Exception:
            return None

    async def _fetch_dataset_qualities(self, session: aiohttp.ClientSession, dataset_id: int) -> Dict[str, Any]:
        """Fetch dataset statistical qualities (instances, features, missing values)."""
        if not self.include_qualities:
            return {}

        url = f"{self.base_url}/data/qualities/{dataset_id}"
        params = {}
        if self.api_key:
            params["api_key"] = self.api_key

        try:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    return {}
                data = await response.json()
                qualities_list = data.get("data_qualities", {}).get("quality", [])
                qualities_dict = {}
                for item in qualities_list:
                    q_name = item.get("name")
                    q_val = item.get("value")
                    if q_name and q_val is not None:
                        qualities_dict[q_name] = q_val
                return qualities_dict
        except Exception:
            return {}

    def _normalize_tags(self, raw_tag: Any) -> List[str]:
        """Normalize OpenML tag field which can be string, list of strings, or None."""
        if not raw_tag:
            return []
        if isinstance(raw_tag, list):
            return [str(t) for t in raw_tag if t]
        return [str(raw_tag)]

    def _build_dataset_document(self, desc: Dict[str, Any], qualities: Dict[str, Any]) -> Dict[str, Any]:
        """Format dataset record into a deterministic, source-cited document."""
        ds_id = desc.get("id")
        name = desc.get("name", "Unknown Dataset")
        version = desc.get("version", "1")
        status = desc.get("status", "active")
        default_target = desc.get("default_target_attribute") or "None declared"
        licence = desc.get("licence") or "Unspecified"
        description = (desc.get("description") or "No description provided.").strip()
        tags = self._normalize_tags(desc.get("tag"))
        citation = desc.get("citation") or "None reported"
        url = desc.get("url") or f"https://www.openml.org/d/{ds_id}"
        original_data_url = desc.get("original_data_url") or "None"

        # Format key qualities
        qualities_summary = []
        if qualities:
            for k in ["NumberOfInstances", "NumberOfFeatures", "NumberOfClasses", "NumberOfMissingValues"]:
                if k in qualities:
                    qualities_summary.append(f"{k}: {qualities[k]}")
            other_q = {k: v for k, v in qualities.items() if k not in ["NumberOfInstances", "NumberOfFeatures", "NumberOfClasses", "NumberOfMissingValues"]}
            if other_q:
                qualities_summary.append(f"Additional metrics reported: {len(other_q)}")
        qualities_str = ", ".join(qualities_summary) if qualities_summary else "No reported qualities"

        clean_text = f"""# OpenML Dataset: {name} (ID: {ds_id}, Version: {version})
Status: {status}
Target Attribute: {default_target}
Licence: {licence}
Tags: {', '.join(tags) if tags else 'None'}
Reported Qualities: {qualities_str}
Citation: {citation}
Source URL: {url}
Original Data URL: {original_data_url}

## Description
{description}
""".strip()

        return {
            "source_id": f"openml:dataset:{ds_id}",
            "dataset_id": ds_id,
            "name": name,
            "version": version,
            "status": status,
            "target_attribute": default_target,
            "licence": licence,
            "tags": tags,
            "qualities": qualities,
            "url": url,
            "clean_text": clean_text,
        }

    async def fetch_dataset(self, dataset_id: int) -> Optional[Dict[str, Any]]:
        """Fetch and normalize a single OpenML dataset record."""
        async with aiohttp.ClientSession() as session:
            desc = await self._fetch_dataset_metadata(session, dataset_id)
            if not desc:
                return None
            qualities = await self._fetch_dataset_qualities(session, dataset_id)
            return self._build_dataset_document(desc, qualities)

    async def fetch_all(self) -> List[Dict[str, Any]]:
        """Fetch and normalize all configured dataset IDs concurrently."""
        if not self.dataset_ids:
            return []

        async with aiohttp.ClientSession() as session:
            tasks = []
            for ds_id in self.dataset_ids:
                tasks.append(self._fetch_single_safe(session, ds_id))
            results = await asyncio.gather(*tasks)
            return [doc for doc in results if doc is not None]

    async def _fetch_single_safe(self, session: aiohttp.ClientSession, dataset_id: int) -> Optional[Dict[str, Any]]:
        desc = await self._fetch_dataset_metadata(session, dataset_id)
        if not desc:
            return None
        qualities = await self._fetch_dataset_qualities(session, dataset_id)
        return self._build_dataset_document(desc, qualities)
