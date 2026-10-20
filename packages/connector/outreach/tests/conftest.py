"""Shared test harness: a fake Outreach API served through a requests adapter.

The real dlt retrying session, RESTClient/rest_api source and ``OutreachAuth`` run against
``FakeOutreach``, so pagination, filters, retries, token refresh and error handling are
exercised end to end with no network access. All fixture data is obviously fake.
"""

import copy
import io
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
import requests
from requests.adapters import HTTPAdapter

# dlt phones home by default; tests must not touch the network.
os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "https://api.outreach.example.test"
BASE_URL = f"{HOST}/api/v2"
TOKEN_URL = f"{HOST}/oauth/token"
FAKE_CLIENT_ID = "fake-client-id"
FAKE_CLIENT_SECRET = "fake-client-secret-do-not-use"
LONG_AGO = 36500  # days: makes the fixtures' 2026-03 activity fall inside any lookback

PATHS = {
    "sequences": "/sequences",
    "prospects": "/prospects",
    "sequence_states": "/sequenceStates",
    "mailings": "/mailings",
    "calls": "/calls",
    "tasks": "/tasks",
    "meetings": "/meetings",
}
FIXTURE_FILES = {
    "/sequences": "sequences",
    "/prospects": "prospects",
    "/sequenceStates": "sequenceStates",
    "/mailings": "mailings",
    "/calls": "calls",
    "/tasks": "tasks",
    "/meetings": "meetings",
}


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def resource(rtype, rid, updated, **attrs):
    """A JSON:API resource object with relationships given as ``rel=id`` keyword attrs."""
    rels = {k[4:]: v for k, v in list(attrs.items()) if k.startswith("rel_")}
    attrs = {k: v for k, v in attrs.items() if not k.startswith("rel_")}
    out = {
        "type": rtype,
        "id": str(rid),
        "attributes": {"createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": updated, **attrs},
        "links": {"self": f"{BASE_URL}/{rtype}s/{rid}"},
    }
    if rels:
        out["relationships"] = {k: {"data": {"type": k, "id": str(v)}} for k, v in rels.items()}
    return out


def make_response(request, adapter, status=200, body=None, headers=None):
    response = requests.Response()
    response.status_code = status
    response.request = request
    response.url = request.url
    response.connection = adapter  # what HTTPAdapter.build_response sets; used by the 401 hook
    response.headers.update(headers or {})
    response.raw = io.BytesIO(json.dumps(body if body is not None else {}).encode())
    response.headers.setdefault("content-type", "application/vnd.api+json")
    return response


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeOutreach(HTTPAdapter):
    """In-memory Outreach: filterable, sortable, link-paginated JSON:API lists + OAuth token."""

    def __init__(self, data=None, page_size=None):
        super().__init__()
        self.data = (
            copy.deepcopy(data)
            if data is not None
            else {path: load(name) for path, name in FIXTURE_FILES.items()}
        )
        self.forced_page_size = page_size
        self.honor_fields = False  # real Outreach trims to fields[type]; off to test the allowlist
        self.calls = []  # (path, params, authorization)
        self.scripted = {}  # path -> list of (status, headers) consumed first
        self.status_for = {}  # (path, "sweep"|"data"|"any") -> status
        self.fail_from_call = {}  # path -> (call number, status)
        self.repeat_page = False
        self.valid_tokens = {"at-0"}
        self.token_calls = []  # grant payloads
        self.token_status = 200
        self.rotate = True
        self.issued = 0

    # -- helpers ---------------------------------------------------------------------
    def paths(self):
        return [p for p, _, _ in self.calls]

    def count(self, path):
        return self.paths().count(path)

    def list_calls(self, path):
        return [params for p, params, _ in self.calls if p == path]

    def sweep_calls(self, path):
        return [
            c
            for c in self.list_calls(path)
            if next((v for k, v in c.items() if k.startswith("fields[")), None) == "updatedAt"
        ]

    def remove(self, path, rid):
        self.data[path] = [r for r in self.data[path] if r["id"] != str(rid)]

    def put(self, path, record):
        self.data[path] = [r for r in self.data[path] if r["id"] != record["id"]] + [record]

    def touch(self, path, rid, updated, **attrs):
        for r in self.data[path]:
            if r["id"] == str(rid):
                r["attributes"].update(attrs, updatedAt=updated)

    # -- transport -------------------------------------------------------------------
    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        if parsed.path == "/oauth/token":
            return self._token(request)
        path = parsed.path.removeprefix("/api/v2")
        params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}
        auth = request.headers.get("Authorization")
        self.calls.append((path, params, auth))
        number = self.count(path)

        queue = self.scripted.get(path)
        if queue:
            status, headers = queue.pop(0)
            return make_response(request, self, status, {"errors": ["scripted"]}, headers)
        if path in self.fail_from_call and number >= self.fail_from_call[path][0]:
            return make_response(request, self, self.fail_from_call[path][1], {"errors": ["x"]})
        is_sweep = (
            "sort" not in params
            and "page[size]" in params
            and params.get("fields[" + path.strip("/").rstrip("s") + "]") == "updatedAt"
        )
        for key in ((path, "sweep" if is_sweep else "data"), (path, "any")):
            if key in self.status_for:
                return make_response(request, self, self.status_for[key], {"errors": ["forced"]})
        if auth not in {f"Bearer {t}" for t in self.valid_tokens}:
            return make_response(request, self, 401, {"errors": ["unauthorized"]})
        if path not in self.data:
            return make_response(request, self, 404, {"errors": ["not found"]})
        return self._list(request, path, params, parsed)

    def _list(self, request, path, params, parsed):
        records = [r for r in self.data[path] if isinstance(r, dict)] + [
            r for r in self.data[path] if not isinstance(r, dict)
        ]
        flt = params.get("filter[updatedAt]")
        if flt:
            floor = _ts(flt.split("..")[0])

            def fresh(r):
                try:
                    return _ts(r["attributes"]["updatedAt"]) >= floor
                except (TypeError, KeyError, ValueError):
                    return True  # malformed rows are always served

            records = [r for r in records if fresh(r)]
        if "filter[id]" in params:
            wanted = params["filter[id]"].split(",")
            records = [r for r in records if r["id"] in wanted]
        if "filter[sequence][id]" in params:
            wanted = params["filter[sequence][id]"].split(",")
            records = [r for r in records if r["relationships"]["sequence"]["data"]["id"] in wanted]
        if "filter[state]" in params:
            wanted = params["filter[state]"].split(",")
            records = [r for r in records if r["attributes"]["state"] in wanted]
        if params.get("sort") == "updatedAt":

            def key(r):
                try:
                    return (_ts(r["attributes"]["updatedAt"]), str(r.get("id")))
                except (TypeError, KeyError, ValueError):
                    return (datetime.min.replace(tzinfo=UTC), str(r.get("id")))

            records.sort(key=key)
        size = self.forced_page_size or int(params.get("page[size]", 50))
        start = int(params.get("page[after]", 0))
        chunk = records[start : start + size]
        fieldset = next((v for k, v in params.items() if k.startswith("fields[")), None)
        if self.honor_fields and fieldset is not None:
            wanted = set(fieldset.split(","))
            chunk = [
                {
                    **r,
                    "attributes": {k: v for k, v in r["attributes"].items() if k in wanted},
                    "relationships": {
                        k: v for k, v in r.get("relationships", {}).items() if k in wanted
                    },
                }
                for r in chunk
            ]
        body = {"data": chunk, "meta": {}}
        nxt = start + size
        if self.repeat_page:
            body["links"] = {"next": request.url}
        elif nxt < len(records):
            query = {k: v for k, v in params.items() if k != "page[after]"}
            query["page[after]"] = nxt
            body["links"] = {"next": f"{HOST}{parsed.path}?{urlencode(query)}"}
        return make_response(request, self, 200, body)

    def _token(self, request):
        form = {k: v[0] for k, v in parse_qs(request.body or "").items()}
        self.token_calls.append(form)
        if self.token_status != 200:
            return make_response(request, self, self.token_status, {"error": "invalid_grant"})
        self.issued += 1
        token = f"at-{self.issued}"
        self.valid_tokens.add(token)
        body = {"access_token": token, "expires_in": 7200, "token_type": "bearer"}
        if self.rotate:
            body["refresh_token"] = f"rt-{self.issued}"
        return make_response(request, self, 200, body)


@pytest.fixture
def fake():
    return FakeOutreach()


@pytest.fixture
def session_for():
    """A real dlt session (retries on, no sleeping, no throttle) around a FakeOutreach."""
    from cognee_community_connector_outreach.outreach import _build_session

    def build(fake_api, **kwargs):
        options = {"backoff_factor": 0, "min_interval": 0, "jitter": 0, "max_attempts": 3} | kwargs
        session = _build_session(**options)
        session.mount("https://", fake_api)
        return session

    return build


@pytest.fixture
def token_path(tmp_path):
    return tmp_path / "tokens" / "outreach_tokens.json"


@pytest.fixture
def seed_tokens(token_path):
    from cognee_community_connector_outreach.outreach import TokenStore

    def seed(access="at-0", refresh="rt-0", expires_in=7200, clock=time.time):
        TokenStore(token_path).save(
            {
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": clock() + expires_in,
                "scope": "x",
            }
        )

    seed()
    return seed


@pytest.fixture
def make_auth(session_for, token_path, seed_tokens):
    from cognee_community_connector_outreach.outreach import build_auth

    def build(fake_api, **kwargs):
        options = {
            "client_id": FAKE_CLIENT_ID,
            "client_secret": FAKE_CLIENT_SECRET,
            "token_store": token_path,
            "token_url": TOKEN_URL,
            "session": session_for(fake_api),
        }
        return build_auth(**(options | kwargs))

    return build


@pytest.fixture
def make_source(session_for, make_auth):
    from cognee_community_connector_outreach import outreach_source

    def build(fake_api, **kwargs):
        defaults = {"page_size": 2, "activity_since": LONG_AGO}
        return outreach_source(
            base_url=BASE_URL,
            session=session_for(fake_api),
            auth=kwargs.pop("auth", None) or make_auth(fake_api),
            **(defaults | kwargs),
        )

    return build


@pytest.fixture
def pipeline_factory(tmp_path):
    """A dlt pipeline into a temp sqlite destination; reusing it keeps dlt state."""
    import dlt

    def build(name="outreach_test"):
        return dlt.pipeline(
            pipeline_name=name,
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / name).as_posix()}.db"),
            dataset_name="outreach_ds",
            pipelines_dir=str(tmp_path / "state"),
        )

    return build


def read_table(pipeline, table, columns="id"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT {columns} FROM {table}") as cursor,
    ):
        return cursor.fetchall()


def ids(pipeline, table):
    return {row[0] for row in read_table(pipeline, table)}


def now_iso(offset_seconds=0):
    return datetime.fromtimestamp(time.time() + offset_seconds, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
