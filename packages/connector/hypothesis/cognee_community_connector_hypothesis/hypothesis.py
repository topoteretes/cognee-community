"""Hypothesis (hypothes.is) web and PDF annotation connector for Cognee.

Ingests user highlights, notes, margins, and tags from Hypothesis REST API
into Cognee's long-term memory layer, grouping annotations by page URL into
searchable, structured Markdown documents.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

HYPOTHESIS_API_URL = "https://api.hypothes.is/api"

try:
    import dlt

    _DLT_AVAILABLE = True
except ImportError:
    dlt = None  # type: ignore
    _DLT_AVAILABLE = False


def _optional_resource(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator applying @dlt.resource if dlt is installed, else returning plain function."""
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if _DLT_AVAILABLE and dlt is not None:
            return dlt.resource(**kwargs)(fn)
        return fn
    return decorator


def _optional_source(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator applying @dlt.source if dlt is installed, else returning plain function."""
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if _DLT_AVAILABLE and dlt is not None:
            return dlt.source(**kwargs)(fn)
        return fn
    return decorator


@dataclass
class HypothesisAnnotation:
    """Represents a single parsed Hypothesis annotation."""
    annotation_id: str
    url: str
    page_title: str
    quote: str
    note: str
    tags: List[str] = field(default_factory=list)
    user: str = ""
    created: str = ""
    updated: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "url": self.url,
            "page_title": self.page_title,
            "quote": self.quote,
            "note": self.note,
            "tags": self.tags,
            "user": self.user,
            "created": self.created,
            "updated": self.updated,
        }


def extract_page_title(item: Dict[str, Any], url: str) -> str:
    """Extract page title from Hypothesis document metadata or fallback to URL."""
    doc = item.get("document", {})
    if doc and isinstance(doc, dict):
        titles = doc.get("title")
        if titles and isinstance(titles, list) and len(titles) > 0 and titles[0]:
            return str(titles[0]).strip()

    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if path_parts:
        return path_parts[-1].replace("-", " ").replace("_", " ").title()
    return parsed.netloc or url


def extract_exact_quote(item: Dict[str, Any]) -> str:
    """Extract the highlighted text quote from selectors."""
    targets = item.get("target", [])
    if not targets or not isinstance(targets, list):
        return ""

    for target in targets:
        if not isinstance(target, dict):
            continue
        selectors = target.get("selector", [])
        if not isinstance(selectors, list):
            continue
        for sel in selectors:
            if isinstance(sel, dict) and sel.get("type") == "TextQuoteSelector":
                return str(sel.get("exact", "")).strip()
    return ""


def parse_raw_annotation(item: Dict[str, Any]) -> Optional[HypothesisAnnotation]:
    """Parse a single raw JSON annotation object from the Hypothesis API."""
    ann_id = item.get("id")
    if not ann_id:
        return None

    url = item.get("uri", "")
    page_title = extract_page_title(item, url)
    quote = extract_exact_quote(item)
    note = str(item.get("text", "")).strip()
    tags = [str(t).strip() for t in item.get("tags", []) if t]
    user = item.get("user", "")
    created = item.get("created", "")
    updated = item.get("updated", "")

    return HypothesisAnnotation(
        annotation_id=ann_id,
        url=url,
        page_title=page_title,
        quote=quote,
        note=note,
        tags=tags,
        user=user,
        created=created,
        updated=updated,
    )


def aggregate_annotations_by_url(
    annotations: List[HypothesisAnnotation],
) -> List[Dict[str, Any]]:
    """Group annotations by target URL into consolidated Markdown documents for Cognee."""
    grouped: Dict[str, List[HypothesisAnnotation]] = {}
    for ann in annotations:
        grouped.setdefault(ann.url, []).append(ann)

    documents: List[Dict[str, Any]] = []

    for url, items in grouped.items():
        if not items:
            continue

        page_title = items[0].page_title or url
        items_sorted = sorted(items, key=lambda x: x.created)

        md_lines = [
            f"# {page_title}",
            f"**Source URL**: [{url}]({url})",
            f"**Total Annotations**: {len(items_sorted)}",
            "",
            "## Highlights & Margin Notes",
            "",
        ]

        all_tags = set()
        for idx, item in enumerate(items_sorted, 1):
            md_lines.append(f"### Annotation {idx}")
            if item.quote:
                quoted_block = "\n".join(f"> {line}" for line in item.quote.splitlines())
                md_lines.append(f"{quoted_block}\n")
            if item.note:
                md_lines.append(f"**Note**: {item.note}\n")
            if item.tags:
                tags_str = ", ".join(f"`#{t}`" for t in item.tags)
                md_lines.append(f"**Tags**: {tags_str}\n")
                all_tags.update(item.tags)
            if item.created:
                md_lines.append(f"*Annotated on*: {item.created}\n")
            md_lines.append("---")

        markdown_body = "\n".join(md_lines)
        data_id = hashlib.sha256(f"{url}:{len(items_sorted)}:{items_sorted[-1].updated}".encode()).hexdigest()

        documents.append({
            "data_id": data_id,
            "url": url,
            "title": page_title,
            "annotation_count": len(items_sorted),
            "tags": sorted(list(all_tags)),
            "content": markdown_body,
            "last_updated": items_sorted[-1].updated,
            "annotations": [it.to_dict() for it in items_sorted],
        })

    return documents


class HypothesisClient:
    """Client for querying the Hypothesis REST API."""

    def __init__(
        self,
        api_token: Optional[str] = None,
        base_url: str = HYPOTHESIS_API_URL,
        client: Optional[httpx.Client] = None,
    ):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        headers = {"User-Agent": "CogneeCommunityHypothesisConnector/0.1.0"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self.client = client or httpx.Client(headers=headers, timeout=30.0)

    def fetch_annotations(
        self,
        username: Optional[str] = None,
        group: Optional[str] = None,
        limit: int = 200,
        max_results: Optional[int] = 1000,
    ) -> List[HypothesisAnnotation]:
        """Fetch annotations matching the query criteria across pages."""
        endpoint = f"{self.base_url}/search"
        params: Dict[str, Any] = {
            "limit": min(limit, 200),
            "sort": "updated",
            "order": "asc",
        }
        if username:
            acct = username if username.startswith("acct:") else f"acct:{username}@hypothes.is"
            params["user"] = acct
        if group:
            params["group"] = group

        results: List[HypothesisAnnotation] = []
        search_after: Optional[str] = None

        while True:
            current_params = dict(params)
            if search_after:
                current_params["search_after"] = search_after

            response = self.client.get(endpoint, params=current_params)
            if response.status_code != 200:
                logger.error(f"Hypothesis API error: HTTP {response.status_code}: {response.text}")
                response.raise_for_status()

            data = response.json()
            rows = data.get("rows", [])
            if not rows:
                break

            for row in rows:
                parsed = parse_raw_annotation(row)
                if parsed:
                    results.append(parsed)

            if max_results and len(results) >= max_results:
                results = results[:max_results]
                break

            if len(rows) < params["limit"]:
                break

            last_item = rows[-1]
            search_after = last_item.get("updated")
            if not search_after:
                break

        return results


@_optional_resource(name="hypothesis_pages", write_disposition="replace")
def hypothesis_pages_resource(
    api_token: Optional[str] = None,
    username: Optional[str] = None,
    group: Optional[str] = None,
    base_url: str = HYPOTHESIS_API_URL,
    max_results: Optional[int] = 1000,
) -> Iterator[Dict[str, Any]]:
    """Yield structured Markdown documents per annotated web page."""
    client = HypothesisClient(api_token=api_token, base_url=base_url)
    annotations = client.fetch_annotations(
        username=username,
        group=group,
        max_results=max_results,
    )
    pages = aggregate_annotations_by_url(annotations)
    for page in pages:
        yield page


@_optional_source(name="hypothesis")
def hypothesis_source(
    api_token: Optional[str] = None,
    username: Optional[str] = None,
    group: Optional[str] = None,
    base_url: str = HYPOTHESIS_API_URL,
    max_results: Optional[int] = 1000,
) -> Any:
    """dlt source for Hypothesis web annotations."""
    res = hypothesis_pages_resource(
        api_token=api_token,
        username=username,
        group=group,
        base_url=base_url,
        max_results=max_results,
    )
    if _DLT_AVAILABLE and dlt is not None:
        return [res]
    return res
