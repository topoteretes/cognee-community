"""Shared test harness: a fake Deel API served through a requests adapter.

The real dlt retrying session and RESTClient run against ``FakeDeel``, so pagination,
retry/backoff and error handling are exercised end to end with no network access.
Everything here is obviously fake data (see ``fixtures/``).
"""

import copy
import io
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from requests.adapters import HTTPAdapter

# dlt phones home by default; tests must not touch the network.
os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://deel.example.test/rest"
FAKE_TOKEN = "fake-token-do-not-use"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def make_response(request, status=200, body=None, headers=None, raw=None):
    response = requests.Response()
    response.status_code = status
    response.request = request
    response.url = request.url
    response.headers.update(headers or {})
    payload = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
    response.raw = io.BytesIO(payload)  # streamed lazily, like a real response
    response.headers.setdefault("content-type", "application/json")
    return response


class FakeDeel(HTTPAdapter):
    """In-memory Deel: cursor-paginated /contracts, offset-paginated /people."""

    def __init__(self, contracts=None, people=None, page_size=None):
        super().__init__()
        self.contracts = copy.deepcopy(
            contracts if contracts is not None else load("contracts.json")
        )
        self.people = copy.deepcopy(people if people is not None else load("people.json"))
        self.forced_page_size = page_size
        self.calls = []  # (path, params, authorization header)
        self.status_for = {}  # path -> status for every request
        self.scripted = {}  # path -> list of (status, headers) consumed first
        self.fail_from_call = {}  # path -> (call number, status): fail from that call on
        self.total_override = {}  # path -> advertised total_rows
        self.repeat_cursor = False
        self.files = {}  # url -> (status, content_type, bytes, headers)
        self.documents = {}  # contract id -> list of document metadata dicts

    def paths(self):
        return [path for path, _, _ in self.calls]

    def count(self, path):
        return self.paths().count(path)

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        path = parsed.path.removeprefix("/rest")
        params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}
        self.calls.append((path, params, request.headers.get("Authorization")))
        number = self.count(path)

        queue = self.scripted.get(path)
        if queue:
            status, headers = queue.pop(0)
            return make_response(request, status, {"error": "scripted"}, headers)
        if path in self.fail_from_call and number >= self.fail_from_call[path][0]:
            return make_response(request, self.fail_from_call[path][1], {"error": "forced"})
        if path in self.status_for:
            return make_response(request, self.status_for[path], {"error": "forced"})

        if path in self.files:
            status, ctype, data, headers = self.files[path]
            return make_response(
                request, status, headers=headers | {"content-type": ctype}, raw=data
            )
        if path.startswith("/eor/contracts/") and path.endswith("/documents"):
            cid = path.split("/")[3]
            return make_response(request, 200, {"data": self.documents.get(cid, [])})
        if path == "/contracts":
            records = self.contracts
            for param, key in (("types", "type"), ("statuses", "status")):
                wanted = params.get(param)
                if wanted:
                    wanted = [wanted] if isinstance(wanted, str) else wanted
                    records = [r for r in records if isinstance(r, dict) and r.get(key) in wanted]
            return self._paged(request, path, records, params, cursor=True)
        if path == "/people":
            return self._paged(request, path, self.people, params, cursor=False)
        return make_response(request, 404, {"error": "not found"})

    def _paged(self, request, path, records, params, cursor):
        limit = self.forced_page_size or int(params.get("limit", 50))
        start = int(params.get("after_cursor" if cursor else "offset", 0) or 0)
        chunk = records[start : start + limit]
        nxt = start + limit
        page = {"total_rows": self.total_override.get(path, len(records))}
        if cursor:
            page["cursor"] = "0" if self.repeat_cursor else str(nxt) if nxt < len(records) else None
        else:
            page.update(offset=start, items_per_page=limit)
        return make_response(request, 200, {"data": chunk, "page": page})


@pytest.fixture
def fake():
    return FakeDeel()


@pytest.fixture
def session_for():
    """Build a real dlt session (retries on, no sleeping, no throttle) around a FakeDeel."""
    from cognee_community_connector_deel.deel import _build_session

    def build(fake_api, **kwargs):
        options = {"backoff_factor": 0, "min_interval": 0, "jitter": 0, "max_attempts": 3} | kwargs
        session = _build_session(**options)
        session.mount("https://", fake_api)
        return session

    return build


@pytest.fixture
def make_source(session_for):
    from cognee_community_connector_deel import deel_source

    def build(fake_api, **kwargs):
        return deel_source(
            token=FAKE_TOKEN, base_url=BASE_URL, session=session_for(fake_api), **kwargs
        )

    return build


@pytest.fixture
def pipeline_factory(tmp_path):
    """A dlt pipeline into a temp sqlite destination; reusing it keeps dlt state."""
    import dlt

    def build(name="deel_test"):
        return dlt.pipeline(
            pipeline_name=name,
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / name).as_posix()}.db"),
            dataset_name="deel_ds",
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
