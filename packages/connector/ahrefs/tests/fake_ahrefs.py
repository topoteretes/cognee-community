"""In-memory Ahrefs API v3, mounted on a ``requests`` session.

The connector talks to it through dlt's real ``rest_api`` code, so the tests
cover the request shape, auth header and data selectors too.  Each endpoint
answers like Ahrefs does: one page, ``limit`` rows at most, and the unit cost
in the ``x-api-units-cost-total-actual`` header.
"""

import json
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import BaseAdapter

BASE_COST = 50


class FakeAhrefs(BaseAdapter):
    def __init__(self):
        super().__init__()
        # target -> [{"date": ..., "domain_rating": ...}]
        self.domain_ratings: dict[str, list[dict]] = {}
        # target -> [backlink dict]
        self.backlinks: dict[str, list[dict]] = {}
        # target -> [keyword dict]
        self.keywords: dict[str, list[dict]] = {}
        # project id -> [issue dict]; a missing project answers 404
        self.issues: dict[str, list[dict]] = {}
        self.usage = {
            "units_limit_api_key": None,
            "units_usage_api_key": 0,
            "units_limit_workspace": None,
            "units_usage_workspace": 0,
        }
        # path -> status code to answer with instead of data
        self.fail: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []

    def session(self) -> requests.Session:
        session = requests.Session()
        session.mount("https://", self)
        return session

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    # -- requests adapter ---------------------------------------------------
    def send(self, request, **_kwargs):
        url = urlparse(request.url)
        path = url.path.removeprefix("/v3/")
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((path, params))

        if request.headers.get("Authorization") != "Bearer test-key":
            return _response(request, 401, {"error": "bad key"})
        if path in self.fail:
            return _response(request, self.fail[path], {"error": "failed"})

        handler = {
            "subscription-info/limits-and-usage": self._usage,
            "site-explorer/domain-rating-history": self._domain_rating,
            "site-explorer/all-backlinks": self._backlinks,
            "site-explorer/organic-keywords": self._keywords,
            "site-audit/issues": self._issues,
        }[path]
        status, body, units = handler(params)
        return _response(request, status, body, units)

    def close(self):
        pass

    # -- endpoints ----------------------------------------------------------
    def _usage(self, _params):
        return 200, {"limits_and_usage": dict(self.usage)}, 0

    def _domain_rating(self, params):
        points = [
            p
            for p in self.domain_ratings.get(params["target"], [])
            if params["date_from"] <= p["date"] <= params.get("date_to", "9999")
        ]
        return 200, {"domain_ratings": points}, _cost(2, len(points))

    def _backlinks(self, params):
        since = params["history"].removeprefix("since:")
        links = [
            link
            for link in self.backlinks.get(params["target"], [])
            if not link.get("is_lost") or link.get("last_seen", "") >= since
        ]
        links.sort(key=lambda link: link.get("domain_rating_source") or 0, reverse=True)
        links = links[: int(params["limit"])]
        fields = params["select"].split(",")
        rows = [{f: link.get(f) for f in fields} for link in links]
        return 200, {"backlinks": rows}, _cost(len(fields), len(rows))

    def _keywords(self, params):
        rows = sorted(self.keywords.get(params["target"], []), key=lambda k: k["best_position"])
        if params.get("country"):
            rows = [k for k in rows if k["keyword_country"] == params["country"]]
        rows = rows[: int(params["limit"])]
        fields = params["select"].split(",")
        rows = [{f: k.get(f) for f in fields} for k in rows]
        return 200, {"keywords": rows}, _cost(len(fields), len(rows))

    def _issues(self, params):
        if params["project_id"] not in self.issues:
            return 404, {"error": "project not found"}, 0
        return 200, {"issues": list(self.issues[params["project_id"]])}, BASE_COST


def _cost(fields: int, rows: int) -> int:
    return max(BASE_COST, fields * rows)


def _response(request, status: int, body: dict, units: int = 0) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers["content-type"] = "application/json"
    response.headers["x-api-units-cost-total-actual"] = str(units)
    response._content = json.dumps(body).encode()
    response.url = request.url
    response.request = request
    response.reason = "OK" if status == 200 else "Error"
    return response
