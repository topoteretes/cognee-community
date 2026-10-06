"""Data models and schemas for Postman Collection v2.1.0 representations.

Defines dataclasses and TypedDicts for collections, folders, requests,
responses, and URLs with defensive parsers supporting Postman schema variations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict


def _parse_description(val: Any) -> str | None:
    """Extract clean string description from string, dict, or None."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        content = val.get("content")
        return str(content) if content is not None else None
    return str(val)


# ---------------------------------------------------------------------------
# TypedDict Definitions (for raw dictionary typing)
# ---------------------------------------------------------------------------


class PostmanCollectionInfoDict(TypedDict, total=False):
    name: str
    schema: str
    _postman_id: str
    id: str
    description: str | dict[str, Any]
    version: str
    updatedAt: str
    createdAt: str


class PostmanUrlDict(TypedDict, total=False):
    raw: str
    protocol: str
    host: list[str] | str
    path: list[str] | str
    port: str
    query: list[dict[str, Any]]
    variable: list[dict[str, Any]]
    hash: str


class PostmanRequestDict(TypedDict, total=False):
    method: str
    url: str | PostmanUrlDict
    header: list[dict[str, Any]]
    body: dict[str, Any]
    description: str | dict[str, Any]
    auth: dict[str, Any]


class PostmanResponseDict(TypedDict, total=False):
    id: str
    name: str
    status: str
    code: int
    header: list[dict[str, str]]
    body: str
    responseTime: int | float


class PostmanItemDict(TypedDict, total=False):
    name: str
    id: str
    _postman_id: str
    description: str | dict[str, Any]
    request: str | PostmanRequestDict
    response: list[PostmanResponseDict]
    item: list[Any]


class PostmanDocumentRow(TypedDict):
    """Row contract required by Cognee document ingestion."""

    id: str
    title: str
    content: str
    url: str | None


# ---------------------------------------------------------------------------
# Dataclass Models (for structured object manipulation)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PostmanCollectionInfo:
    """Metadata describing a Postman collection."""

    name: str
    schema: str = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
    postman_id: str | None = None
    description: str | None = None
    version: str | None = None
    updated_at: str | None = None
    created_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostmanCollectionInfo:
        if not isinstance(data, dict):
            return cls(name="")
        return cls(
            name=data.get("name", ""),
            schema=data.get(
                "schema",
                "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            ),
            postman_id=data.get("_postman_id") or data.get("id"),
            description=_parse_description(data.get("description")),
            version=str(data["version"]) if data.get("version") else None,
            updated_at=data.get("updatedAt"),
            created_at=data.get("createdAt"),
        )


@dataclass(slots=True)
class PostmanUrl:
    """Represents a Postman URL (raw string or structured object)."""

    raw: str = ""
    protocol: str | None = None
    host: list[str] | str | None = None
    path: list[str] | str | None = None
    port: str | None = None
    query: list[dict[str, Any]] = field(default_factory=list)
    variable: list[dict[str, Any]] = field(default_factory=list)
    hash: str | None = None

    @classmethod
    def from_dict(cls, data: str | dict[str, Any] | None) -> PostmanUrl:
        if data is None:
            return cls(raw="")
        if isinstance(data, str):
            return cls(raw=data)
        if not isinstance(data, dict):
            return cls(raw=str(data))
        return cls(
            raw=data.get("raw", ""),
            protocol=data.get("protocol"),
            host=data.get("host"),
            path=data.get("path"),
            port=str(data["port"]) if data.get("port") is not None else None,
            query=data.get("query", []) if isinstance(data.get("query"), list) else [],
            variable=data.get("variable", []) if isinstance(data.get("variable"), list) else [],
            hash=data.get("hash"),
        )

    @property
    def full_url(self) -> str:
        """Return raw URL or reconstruct canonical URL string."""
        if self.raw:
            return self.raw
        proto = f"{self.protocol}://" if self.protocol else ""
        host_str = ".".join(self.host) if isinstance(self.host, list) else (self.host or "")
        path_str = "/".join(self.path) if isinstance(self.path, list) else (self.path or "")
        if path_str and not path_str.startswith("/"):
            path_str = f"/{path_str}"
        port_str = f":{self.port}" if self.port else ""
        query_str = ""
        if self.query:
            query_items: list[str] = []
            for q in self.query:
                if isinstance(q, dict) and "key" in q:
                    k = str(q.get("key", ""))
                    v = str(q.get("value", ""))
                    query_items.append(f"{k}={v}" if v else k)
            if query_items:
                query_str = f"?{'&'.join(query_items)}"
        return f"{proto}{host_str}{port_str}{path_str}{query_str}"


@dataclass(slots=True)
class PostmanResponse:
    """Sample response associated with an HTTP request."""

    name: str = ""
    id: str | None = None
    status: str | None = None
    code: int | None = None
    header: list[dict[str, str]] = field(default_factory=list)
    body: str | None = None
    response_time: int | float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostmanResponse:
        if not isinstance(data, dict):
            return cls()
        headers: list[dict[str, str]] = []
        raw_headers = data.get("header")
        if isinstance(raw_headers, list):
            for h in raw_headers:
                if isinstance(h, dict):
                    headers.append(
                        {
                            "key": str(h.get("key", "")),
                            "value": str(h.get("value", "")),
                        }
                    )
                elif isinstance(h, str) and ":" in h:
                    k, v = h.split(":", 1)
                    headers.append({"key": k.strip(), "value": v.strip()})
        return cls(
            name=data.get("name", ""),
            id=data.get("id"),
            status=data.get("status"),
            code=data.get("code"),
            header=headers,
            body=data.get("body"),
            response_time=data.get("responseTime"),
        )


@dataclass(slots=True)
class PostmanRequest:
    """HTTP request specification within a collection item."""

    method: str = "GET"
    url: PostmanUrl = field(default_factory=PostmanUrl)
    header: list[dict[str, Any]] = field(default_factory=list)
    body: dict[str, Any] | None = None
    description: str | None = None
    auth: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: str | dict[str, Any] | None) -> PostmanRequest:
        if data is None:
            return cls()
        if isinstance(data, str):
            return cls(method="GET", url=PostmanUrl.from_dict(data))
        if not isinstance(data, dict):
            return cls()
        raw_method = data.get("method", "GET")
        method = raw_method.upper() if isinstance(raw_method, str) else "GET"
        url = PostmanUrl.from_dict(data.get("url"))
        headers = data.get("header", []) if isinstance(data.get("header"), list) else []
        body = data.get("body") if isinstance(data.get("body"), dict) else None
        desc = _parse_description(data.get("description"))
        auth = data.get("auth") if isinstance(data.get("auth"), dict) else None
        return cls(
            method=method,
            url=url,
            header=headers,
            body=body,
            description=desc,
            auth=auth,
        )

    @property
    def body_mode(self) -> str | None:
        """Return body mode (e.g. raw, urlencoded, formdata, graphql)."""
        if self.body and isinstance(self.body, dict):
            return self.body.get("mode")
        return None

    @property
    def body_raw(self) -> str | None:
        """Return raw body payload string if present."""
        if self.body and isinstance(self.body, dict):
            return self.body.get("raw")
        return None

    @property
    def url_str(self) -> str:
        """Return canonical URL string."""
        return self.url.full_url


@dataclass(slots=True)
class PostmanCollectionItem:
    """An item within a collection, representing either a Folder or a Request."""

    name: str = ""
    id: str | None = None
    description: str | None = None
    request: PostmanRequest | None = None
    response: list[PostmanResponse] = field(default_factory=list)
    item: list[PostmanCollectionItem] = field(default_factory=list)
    auth: dict[str, Any] | None = None
    variable: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostmanCollectionItem:
        if not isinstance(data, dict):
            return cls()
        name = data.get("name", "")
        item_id = data.get("id") or data.get("_postman_id")
        desc = _parse_description(data.get("description"))

        raw_req = data.get("request")
        req = PostmanRequest.from_dict(raw_req) if raw_req is not None else None

        raw_responses = data.get("response", [])
        responses = (
            [PostmanResponse.from_dict(r) for r in raw_responses if isinstance(r, dict)]
            if isinstance(raw_responses, list)
            else []
        )

        raw_items = data.get("item", [])
        children = (
            [cls.from_dict(c) for c in raw_items if isinstance(c, dict)]
            if isinstance(raw_items, list)
            else []
        )

        auth = data.get("auth") if isinstance(data.get("auth"), dict) else None
        variables = data.get("variable", []) if isinstance(data.get("variable"), list) else []

        return cls(
            name=name,
            id=item_id,
            description=desc,
            request=req,
            response=responses,
            item=children,
            auth=auth,
            variable=variables,
        )

    @property
    def is_folder(self) -> bool:
        """Return True if item represents a folder containing sub-items."""
        return self.request is None and len(self.item) > 0

    @property
    def is_request(self) -> bool:
        """Return True if item represents an HTTP request."""
        return self.request is not None


@dataclass(slots=True)
class PostmanCollection:
    """Root Postman collection object."""

    info: PostmanCollectionInfo
    item: list[PostmanCollectionItem] = field(default_factory=list)
    auth: dict[str, Any] | None = None
    variable: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostmanCollection:
        if not isinstance(data, dict):
            return cls(info=PostmanCollectionInfo(name=""))
        col_data = data.get("collection", data) if isinstance(data, dict) else {}
        info_data = col_data.get("info", {}) if isinstance(col_data, dict) else {}
        info = PostmanCollectionInfo.from_dict(info_data)
        raw_items = col_data.get("item", []) if isinstance(col_data, dict) else []
        items = (
            [PostmanCollectionItem.from_dict(it) for it in raw_items if isinstance(it, dict)]
            if isinstance(raw_items, list)
            else []
        )
        auth = col_data.get("auth") if isinstance(col_data, dict) else None
        variables = (
            col_data.get("variable", [])
            if isinstance(col_data, dict) and isinstance(col_data.get("variable"), list)
            else []
        )
        return cls(
            info=info,
            item=items,
            auth=auth,
            variable=variables,
        )
