"""Unit tests for the xkcd dlt connector.

Two layers, all runnable in CI without network access:

* DB-free tests for comic→row rendering, the HTTP client (retry/pacing, the
  #404 gap), and the generic document DataItem tagging (``source="xkcd"``)
  that routes comics through normal cognify.
* dlt-pipeline tests (fake client, temp sqlite destination) covering the
  acceptance criteria: first-run backfill, the #404 gap, incremental
  delta-only re-sync (merge keeps the corpus), and abort-on-error.
"""

import logging
import time
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_xkcd.xkcd import (
    XKCD_SOURCE_NAME,
    XkcdClient,
    _comic_date,
    _comic_title,
    _comic_to_row,
    _render_comic,
    _retry_after,
    xkcd_source,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _payload(num=614, **overrides):
    """A minimal comic payload shaped like the xkcd JSON API."""
    payload = {
        "num": num,
        "title": f"Comic {num}",
        "safe_title": f"Comic {num}",
        "alt": f"alt {num}",
        "transcript": f"transcript {num}",
        "img": f"https://imgs.xkcd.com/comics/{num}.png",
        "year": "2024",
        "month": "1",
        "day": "1",
    }
    payload.update(overrides)
    return payload


class FakeXkcdClient:
    """Stand-in for XkcdClient backed by a synthetic, growing archive."""

    def __init__(self, latest, missing=(), fail_on=None):
        self.latest = latest
        self.missing = set(missing)
        self.fail_on = fail_on
        self.comic_calls = []

    def latest_num(self):
        return self.latest

    def comic(self, num):
        self.comic_calls.append(num)
        if num == self.fail_on:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://xkcd.com/"))
        if num in self.missing:
            return None
        return _payload(num)


# ---------------------------------------------------------------------------
# Comic → row rendering (DB-free)
# ---------------------------------------------------------------------------


def test_comic_to_row_flattens_comic():
    row = _comic_to_row(_payload())

    assert row["id"] == "614"
    assert row["num"] == 614
    assert row["url"] == "https://xkcd.com/614/"
    assert row["title"] == "Comic 614"
    assert "Published: 2024-01-01" in row["content"]
    assert "Image: https://imgs.xkcd.com/comics/614.png" in row["content"]
    assert "Alt text: alt 614" in row["content"]
    assert "Transcript:\ntranscript 614" in row["content"]


def test_comic_to_row_has_only_stable_fields():
    # No fetch timestamps (or other volatile fields), so an unchanged comic
    # keeps a stable content-hash data_id and is never re-cognified.
    row = _comic_to_row(_payload())
    assert set(row) == {"id", "num", "url", "title", "content"}


def test_comic_title_prefers_safe_title():
    assert _comic_title({"safe_title": "Safe", "title": "Raw"}, 1) == "Safe"
    assert _comic_title({"title": "  Raw  "}, 1) == "Raw"


def test_comic_title_falls_back_to_comic_number():
    assert _comic_title({}, 7) == "xkcd #7"
    assert _comic_title({"safe_title": "   ", "title": None}, 7) == "xkcd #7"


def test_comic_date_formats_iso():
    assert _comic_date({"year": "2009", "month": "9", "day": "11"}) == "2009-09-11"
    assert _comic_date({"year": 2024, "month": 1, "day": 5}) == "2024-01-05"


def test_comic_date_missing_or_invalid_is_blank():
    assert _comic_date({}) == ""
    assert _comic_date({"year": "2009", "month": None, "day": "11"}) == ""
    assert _comic_date({"year": "x", "month": "9", "day": "1"}) == ""


def test_render_comic_omits_empty_sections():
    assert _render_comic({"num": 1}) == ""
    assert _render_comic({"alt": "only alt"}) == "Alt text: only alt"
    assert "Transcript" not in _render_comic({"transcript": "   "})


def test_render_comic_keeps_multiline_transcript():
    rendered = _render_comic(_payload(transcript="LINE 1\nLINE 2"))
    assert "Transcript:\nLINE 1\nLINE 2" in rendered


# ---------------------------------------------------------------------------
# HTTP client: retry, pacing, 404 (DB-free, fake session)
# ---------------------------------------------------------------------------


class FakeSession:
    """httpx.Client stand-in replaying a queue of responses/exceptions."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def _response(status, *, json=None, headers=None):
    request = httpx.Request("GET", "https://xkcd.com/1/info.0.json")
    return httpx.Response(status, json=json, headers=headers, request=request)


def test_latest_num_returns_num():
    session = FakeSession(_response(200, json={"num": 3307}))
    client = XkcdClient(session=session, min_interval=0)
    assert client.latest_num() == 3307


def test_comic_returns_payload_and_treats_unknown_404_as_missing():
    session = FakeSession(_response(200, json={"num": 614}), _response(404, json={}))
    client = XkcdClient(session=session, min_interval=0)
    assert client.comic(614)["num"] == 614
    assert client.comic(99999) is None  # a gap must not abort a sync


def test_comic_404_gap_never_hits_the_network():
    session = FakeSession()
    client = XkcdClient(session=session, min_interval=0)
    assert client.comic(404) is None
    assert session.calls == []


def test_client_retries_429_and_5xx_honoring_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    session = FakeSession(
        _response(429, headers={"Retry-After": "0.25"}),
        _response(500, json={}),
        _response(200, json={"num": 1}),
    )
    client = XkcdClient(session=session, min_interval=0)

    assert client.comic(1)["num"] == 1
    assert slept == [0.25, 2.0]  # Retry-After honored, then plain backoff
    assert len(session.calls) == 3


def test_client_retries_transport_errors(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    session = FakeSession(
        httpx.ConnectError("boom", request=httpx.Request("GET", "https://xkcd.com/1/info.0.json")),
        _response(200, json={"num": 1}),
    )
    client = XkcdClient(session=session, min_interval=0)

    assert client.comic(1)["num"] == 1
    assert slept == [1.0]


def test_client_gives_up_after_max_retries():
    session = FakeSession(*[_response(500, json={}) for _ in range(XkcdClient._MAX_RETRIES)])
    client = XkcdClient(session=session, min_interval=0)

    with pytest.raises(httpx.HTTPStatusError):
        client.comic(1)
    assert len(session.calls) == XkcdClient._MAX_RETRIES


def test_client_permanent_error_is_not_retried():
    session = FakeSession(_response(403, json={}))
    client = XkcdClient(session=session, min_interval=0)

    with pytest.raises(httpx.HTTPStatusError):
        client.comic(1)
    assert len(session.calls) == 1


def test_client_paces_consecutive_requests(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    session = FakeSession(_response(200, json={"num": 1}), _response(200, json={"num": 2}))
    client = XkcdClient(session=session, min_interval=5.0)

    client.comic(1)
    client.comic(2)

    # The first request goes out immediately; the second waits out the interval.
    assert len(slept) == 1
    assert 0 < slept[0] <= 5.0


def test_retry_after_header_or_exponential_backoff():
    assert _retry_after({"retry-after": "3"}, 0) == 3.0
    assert _retry_after({"Retry-After": "0.5"}, 0) == 0.5
    assert _retry_after({"retry-after": "not-a-number"}, 1) == 2.0
    assert _retry_after(None, 2) == 4.0


# ---------------------------------------------------------------------------
# Row → DataItem tagging (DB-free)
# ---------------------------------------------------------------------------


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "614",
            "url": "https://xkcd.com/614/",
            "title": "Verizon Math",
            "content": "Alt text: ...",
        },
        table_name="xkcd_comics",
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "614")

    item = _build_document_data_item(row, data_id, "xkcd")

    # source="xkcd" (not "dlt") is what routes the comic through normal cognify.
    assert item.system_metadata["source"] == "xkcd"
    assert item.system_metadata["url"] == "https://xkcd.com/614/"
    assert item.system_metadata["external_id"] == "614"
    assert item.system_metadata["table_name"] == "xkcd_comics"
    assert item.data_id == data_id
    assert item.data.startswith("# Verizon Math")
    assert "Alt text: ..." in item.data


def test_xkcd_source_declares_document_marker():
    # resolve_dlt_sources routes on the document-source marker (not on this name),
    # but the tag it carries is the source name; keep it stable.
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = xkcd_source(client=FakeXkcdClient(latest=0))
    assert XKCD_SOURCE_NAME == "xkcd"
    assert document_source_tag(source) == "xkcd"


# ---------------------------------------------------------------------------
# dlt pipeline: backfill, incremental re-sync, merge (needs dlt + sqlalchemy)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _make_pipeline(dlt, tmp_path):
    db_path = (tmp_path / "xkcd.db").as_posix()
    return dlt.pipeline(
        pipeline_name="xkcd_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="xkcd_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _read_comics(pipeline):
    """Return {id: row-dict} for the xkcd_comics table.

    Reads positionally (the SELECT fixes the column order) since dlt's
    sqlalchemy cursor exposes a SQLAlchemy Result without DB-API ``description``.
    """
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, num, title, content FROM xkcd_comics") as cursor,
    ):
        rows = cursor.fetchall()
    return {
        row[0]: {"id": row[0], "num": row[1], "title": row[2], "content": row[3]} for row in rows
    }


def test_first_sync_backfills_archive(dlt_mod, tmp_path):
    pipeline = _make_pipeline(dlt_mod, tmp_path)
    fake = FakeXkcdClient(latest=5)

    pipeline.run(xkcd_source(client=fake))

    rows = _read_comics(pipeline)
    assert set(rows) == {"1", "2", "3", "4", "5"}
    assert fake.comic_calls == [1, 2, 3, 4, 5]
    assert "Published: 2024-01-01" in rows["1"]["content"]
    assert rows["1"]["title"] == "Comic 1"


def test_missing_comic_is_skipped(dlt_mod, tmp_path):
    pipeline = _make_pipeline(dlt_mod, tmp_path)
    fake = FakeXkcdClient(latest=5, missing={3})

    pipeline.run(xkcd_source(client=fake))

    rows = _read_comics(pipeline)
    assert set(rows) == {"1", "2", "4", "5"}
    assert fake.comic_calls == [1, 2, 3, 4, 5]  # the gap was probed and skipped


def test_resync_fetches_only_delta_and_merge_keeps_corpus(dlt_mod, tmp_path):
    pipeline = _make_pipeline(dlt_mod, tmp_path)
    fake = FakeXkcdClient(latest=5)
    pipeline.run(xkcd_source(client=fake))

    fake.latest = 7
    pipeline.run(xkcd_source(client=fake))

    rows = _read_comics(pipeline)
    assert set(rows) == {"1", "2", "3", "4", "5", "6", "7"}
    # Only the delta was fetched — the corpus is kept by merge, not re-read.
    assert fake.comic_calls == [1, 2, 3, 4, 5, 6, 7]


def test_resync_with_no_new_comics_is_a_noop(dlt_mod, tmp_path):
    pipeline = _make_pipeline(dlt_mod, tmp_path)
    fake = FakeXkcdClient(latest=5)
    pipeline.run(xkcd_source(client=fake))

    pipeline.run(xkcd_source(client=fake))

    assert fake.comic_calls == [1, 2, 3, 4, 5]
    assert set(_read_comics(pipeline)) == {"1", "2", "3", "4", "5"}


def test_since_num_bounds_first_backfill_only(dlt_mod, tmp_path):
    pipeline = _make_pipeline(dlt_mod, tmp_path)
    fake = FakeXkcdClient(latest=6)

    pipeline.run(xkcd_source(since_num=3, client=fake))
    assert fake.comic_calls == [4, 5, 6]
    assert set(_read_comics(pipeline)) == {"4", "5", "6"}

    # Once a watermark exists, it wins over the (now stale) since_num seed.
    fake.latest = 8
    pipeline.run(xkcd_source(since_num=100, client=fake))
    assert fake.comic_calls == [4, 5, 6, 7, 8]
    assert set(_read_comics(pipeline)) == {"4", "5", "6", "7", "8"}


def test_rollback_behind_watermark_warns_but_drops_nothing(dlt_mod, tmp_path, caplog):
    pipeline = _make_pipeline(dlt_mod, tmp_path)
    fake = FakeXkcdClient(latest=5)
    pipeline.run(xkcd_source(client=fake))

    # Simulate upstream rolling back (should never happen upstream): the
    # connector warns, fetches nothing new, and deletes nothing.
    fake.latest = 4
    with caplog.at_level(logging.WARNING):
        pipeline.run(xkcd_source(client=fake))

    assert "behind the stored watermark" in caplog.text
    assert set(_read_comics(pipeline)) == {"1", "2", "3", "4", "5"}


def test_fetch_error_aborts_sync(dlt_mod, tmp_path):
    # A mid-fetch failure must abort the run (nothing is loaded) rather than
    # commit a partial snapshot that orphan cleanup would reconcile as deletes.
    pipeline = _make_pipeline(dlt_mod, tmp_path)
    fake = FakeXkcdClient(latest=10, fail_on=5)

    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        pipeline.run(xkcd_source(client=fake))
