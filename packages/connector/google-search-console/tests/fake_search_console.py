"""An in-memory Search Console API (and Google token endpoint) behind ``httpx.MockTransport``.

It follows the documented behaviour the connector relies on: bearer auth (401), percent-encoded
property URLs, ``searchAnalytics.query`` aggregating by the requested dimensions, rows sorted by
clicks, ``rowLimit`` capped at 25,000 with ``startRow`` paging, ``dataState="final"`` hiding the
newest days, rare queries left out of query rows but counted in totals, and the
``{error: {code, message, errors: [{reason}]}}`` body. Tests drive it like a site owner: add
traffic, revise it, remove a property or change a permission.
"""

import json
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import unquote

import httpx

ACCESS_TOKEN = "test-access-token"
REFRESH_TOKEN = "test-refresh-token"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
# Search Console's newest final day is this many days before today.
FINAL_LAG_DAYS = 2
_DIMENSIONS = ("query", "page", "country", "device", "date")


class FakeSearchConsole:
    def __init__(self, today: date) -> None:
        self.today = today
        # site url -> permission level
        self.sites: dict[str, str] = {}
        # site url -> fact rows: date, query (None = a rare query Google withholds), page, ...
        self.facts: dict[str, list[dict]] = defaultdict(list)
        self.valid_token = ACCESS_TOKEN
        self.calls: list[tuple[str, str, dict]] = []
        self.token_requests = 0
        # path fragment -> responses to give before answering normally.
        self._failures: dict[str, list[httpx.Response]] = {}

    # -- owner actions -------------------------------------------------------
    def add_site(self, url="sc-domain:example.com", permission="siteOwner") -> str:
        self.sites[url] = permission
        return url

    def remove_site(self, url) -> None:
        del self.sites[url]
        self.facts.pop(url, None)

    def set_permission(self, url, permission) -> None:
        self.sites[url] = permission

    def add_traffic(
        self,
        url,
        day: date,
        query,
        page,
        *,
        clicks=1,
        impressions=10,
        position=3.0,
        country="usa",
        device="MOBILE",
    ) -> None:
        self.facts[url].append(
            {
                "date": day.isoformat(),
                "query": query,
                "page": page,
                "country": country,
                "device": device,
                "clicks": clicks,
                "impressions": impressions,
                "position": position,
            }
        )

    def fail_next(self, path_fragment: str, *responses: httpx.Response) -> None:
        self._failures.setdefault(path_fragment, []).extend(responses)

    # -- inspection ----------------------------------------------------------
    def queries_to(self, fragment: str) -> list[dict]:
        return [body for method, path, body in self.calls if fragment in path and method == "POST"]

    @property
    def final_through(self) -> date:
        return self.today - timedelta(days=FINAL_LAG_DAYS)

    # -- the HTTP surface ----------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return self._token(request)
        path = request.url.raw_path.decode().split("?")[0]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        for fragment, queue in self._failures.items():
            if fragment in path and queue:
                return queue.pop(0)
        if request.headers.get("authorization") != f"Bearer {self.valid_token}":
            return _error(401, "Request had invalid authentication credentials.", "authError")
        prefix = "/webmasters/v3/sites"
        if path == prefix:
            entries = [{"siteUrl": u, "permissionLevel": p} for u, p in self.sites.items()]
            return httpx.Response(200, json={"siteEntry": entries})
        # Property URLs arrive percent-encoded in a single path segment.
        rest = path.removeprefix(prefix + "/")
        site_part, _, tail = rest.partition("/searchAnalytics/query")
        if "/" in site_part:
            return _error(404, "Not Found", "notFound")
        url = unquote(site_part)
        if url not in self.sites:
            return _error(404, "Site not found", "notFound")
        if not tail and request.method == "GET":
            return httpx.Response(200, json={"siteUrl": url, "permissionLevel": self.sites[url]})
        if self.sites[url] == "siteUnverifiedUser":
            return _error(403, "User does not have sufficient permission for site.", "forbidden")
        return self._query(url, body)

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        form = dict(item.split("=", 1) for item in request.content.decode().split("&"))
        if form.get("refresh_token") != REFRESH_TOKEN or unquote(form.get("client_id", "")) != (
            CLIENT_ID
        ):
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "Bad Request"}
            )
        self.valid_token = f"{ACCESS_TOKEN}-{self.token_requests}"
        return httpx.Response(200, json={"access_token": self.valid_token, "expires_in": 3599})

    def _query(self, url: str, body: dict) -> httpx.Response:
        dims = body.get("dimensions") or []
        limit, start = body.get("rowLimit", 1000), body.get("startRow", 0)
        if not 1 <= limit <= 25_000 or any(d not in _DIMENSIONS for d in dims):
            return _error(400, "Invalid value at 'rowLimit' or 'dimensions'.", "invalidParameter")
        newest = body["endDate"]
        if body.get("dataState") == "final":
            newest = min(newest, self.final_through.isoformat())
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for fact in self.facts[url]:
            if not body["startDate"] <= fact["date"] <= newest:
                continue
            # Rare queries are left out of rows that name the query, but count in totals.
            if "query" in dims and fact["query"] is None:
                continue
            groups[tuple(fact[d] for d in dims)].append(fact)
        rows = []
        for keys, facts in groups.items():
            clicks = sum(f["clicks"] for f in facts)
            impressions = sum(f["impressions"] for f in facts)
            row = {
                "clicks": clicks,
                "impressions": impressions,
                "ctr": clicks / impressions if impressions else 0,
                "position": sum(f["position"] * f["impressions"] for f in facts) / impressions
                if impressions
                else 0,
            }
            rows.append({"keys": list(keys), **row} if dims else row)
        rows.sort(key=lambda r: (-r["clicks"], -r["impressions"], r.get("keys", [])))
        page = rows[start : start + limit]
        return httpx.Response(200, json={"rows": page} if page else {})


def _error(status: int, message: str, reason: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": status, "message": message, "errors": [{"reason": reason}]}},
    )
