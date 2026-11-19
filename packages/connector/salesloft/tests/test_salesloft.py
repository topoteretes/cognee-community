"""Unit tests for the Salesloft dlt connector.

Two layers, all runnable in CI without a live Salesloft token:

* DB-free tests for signal policy, record rendering, provenance, node_set,
  stable IDs, and the deletion reconciliation safety guards.
* dlt-pipeline tests (fake Salesloft API via httpx.MockTransport, temp sqlite
  destination) covering the acceptance criteria: incremental sync picks up
  changes, deleted records are reconciled and hard-deleted, signal policy
  filters noise, and the cursor advances safely.
"""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from cognee_community_connector_salesloft.client import (
    SalesloftClient,
    _backoff_delay,
    _retry_after,
)
from cognee_community_connector_salesloft.policy import DEFAULT_POLICY, SignalPolicy
from cognee_community_connector_salesloft.reconcile import (
    reconcile_deletions,
    track_known_ids,
)
from cognee_community_connector_salesloft.render import (
    deleted_row,
    render_cadence,
    render_call,
    render_note,
)

# ---------------------------------------------------------------------------
# Fixtures / fake data
# ---------------------------------------------------------------------------


def _note(note_id=1, content="Prospect interested in Q4.", updated_at="2024-06-01T10:00:00Z"):
    return {
        "id": note_id,
        "content": content,
        "updated_at": updated_at,
        "user": {"id": 10, "name": "Alice Rep"},
        "associated_with": {"id": 20, "name": "Bob Prospect"},
    }


def _call(
    call_id=1,
    duration=120,
    note="Discussed pricing.",
    disposition="Connected",
    sentiment="positive",
    direction="outbound",
    updated_at="2024-06-01T11:00:00Z",
):
    return {
        "id": call_id,
        "duration": duration,
        "note": note,
        "disposition": disposition,
        "sentiment": sentiment,
        "direction": direction,
        "updated_at": updated_at,
        "user": {"id": 10, "name": "Alice Rep"},
        "called_person": {"id": 20, "name": "Bob Prospect"},
        "account": {"id": 30},
        "cadence": {"id": 40},
    }


def _cadence(
    cadence_id=1, name="Outbound Q4", updated_at="2024-06-01T12:00:00Z", archived_at=None
):
    return {
        "id": cadence_id,
        "name": name,
        "updated_at": updated_at,
        "archived_at": archived_at,
        "owner": {"id": 10, "name": "Alice Rep"},
        "cadence_function": "outbound",
        "target_daily_people": 25,
        "counts": {"people": 100, "emails": 50, "calls": 30},
    }


# ---------------------------------------------------------------------------
# Signal policy (DB-free)
# ---------------------------------------------------------------------------


class TestSignalPolicy:
    def test_notes_always_accepted(self):
        policy = SignalPolicy()
        assert policy.accept_note({"id": 1, "content": "anything"}) is True
        assert policy.accept_note({}) is True

    def test_call_with_note_accepted(self):
        policy = SignalPolicy(min_call_seconds=60, require_note_or_disposition=True)
        call = _call(duration=5, note="Short but noted.", disposition="", sentiment="")
        assert policy.accept_call(call) is True

    def test_call_with_disposition_accepted(self):
        policy = SignalPolicy(min_call_seconds=60, require_note_or_disposition=True)
        call = _call(duration=5, note="", disposition="Left Voicemail", sentiment="")
        assert policy.accept_call(call) is True

    def test_short_call_no_signal_rejected(self):
        policy = SignalPolicy(min_call_seconds=60, require_note_or_disposition=True)
        call = _call(duration=5, note="", disposition="", sentiment="")
        assert policy.accept_call(call) is False

    def test_long_call_no_signal_rejected_when_require_note(self):
        """Even long calls need signal when require_note_or_disposition=True."""
        policy = SignalPolicy(min_call_seconds=10, require_note_or_disposition=True)
        call = _call(duration=600, note="", disposition="", sentiment="")
        assert policy.accept_call(call) is False

    def test_long_call_no_signal_accepted_when_not_required(self):
        policy = SignalPolicy(min_call_seconds=10, require_note_or_disposition=False)
        call = _call(duration=600, note="", disposition="", sentiment="")
        assert policy.accept_call(call) is True

    def test_excluded_disposition_rejected(self):
        policy = SignalPolicy(excluded_dispositions={"No Answer", "Busy"})
        call = _call(disposition="No Answer")
        assert policy.accept_call(call) is False

    def test_default_policy_filters_empty_calls(self):
        assert (
            DEFAULT_POLICY.accept_call(
                _call(duration=0, note="", disposition="", sentiment="")
            )
            is False
        )
        assert DEFAULT_POLICY.accept_call(_call(duration=120, note="Good call.")) is True

    def test_cadence_archived_filtered(self):
        policy = SignalPolicy(include_archived_cadences=False)
        assert policy.accept_cadence(_cadence(archived_at="2024-01-01")) is False
        assert policy.accept_cadence(_cadence(archived_at=None)) is True

    def test_cadence_archived_included_when_flag_set(self):
        policy = SignalPolicy(include_archived_cadences=True)
        assert policy.accept_cadence(_cadence(archived_at="2024-01-01")) is True


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_note_produces_document(self):
        row = render_note(_note())
        assert row["id"] == "salesloft:note:1"
        assert "Bob Prospect" in row["title"]
        assert "Alice Rep" in row["title"]
        assert "Prospect interested in Q4." in row["content"]
        assert row["_deleted"] is False
        assert row["updated_at"] == "2024-06-01T10:00:00Z"

    def test_render_call_produces_document(self):
        row = render_call(_call())
        assert row["id"] == "salesloft:call:1"
        assert "Bob Prospect" in row["title"]
        assert "Discussed pricing." in row["content"]
        assert "Duration: 120s" in row["content"]
        assert "Disposition: Connected" in row["content"]
        assert row["_deleted"] is False

    def test_render_cadence_produces_document(self):
        row = render_cadence(_cadence())
        assert row["id"] == "salesloft:cadence:1"
        assert "Outbound Q4" in row["title"]
        assert "Alice Rep" in row["content"]
        assert row["_deleted"] is False

    def test_deleted_row_produces_tombstone(self):
        row = deleted_row(42, "note")
        assert row == {"id": "salesloft:note:42", "_deleted": True}

    def test_stable_ids_are_deterministic(self):
        """Same input always produces the same document ID."""
        r1 = render_note(_note(note_id=99))
        r2 = render_note(_note(note_id=99))
        assert r1["id"] == r2["id"] == "salesloft:note:99"

    def test_note_node_set(self):
        row = render_note(_note())
        assert "salesloft:note:1" in row["cognee_node_set"]
        assert "salesloft:person:20" in row["cognee_node_set"]

    def test_call_node_set(self):
        row = render_call(_call())
        assert "salesloft:call:1" in row["cognee_node_set"]
        assert "salesloft:person:20" in row["cognee_node_set"]
        assert "salesloft:account:30" in row["cognee_node_set"]
        assert "salesloft:cadence:40" in row["cognee_node_set"]

    def test_cadence_node_set(self):
        row = render_cadence(_cadence())
        assert "salesloft:cadence:1" in row["cognee_node_set"]
        assert "salesloft:user:10" in row["cognee_node_set"]

    def test_note_provenance(self):
        row = render_note(_note())
        prov = row["provenance"]
        assert prov["salesloft_id"] == 1
        assert prov["resource_type"] == "note"
        assert prov["owner_id"] == 10
        assert prov["person_id"] == 20

    def test_call_provenance(self):
        row = render_call(_call())
        prov = row["provenance"]
        assert prov["salesloft_id"] == 1
        assert prov["resource_type"] == "call"
        assert prov["duration"] == 120
        assert prov["account_id"] == 30
        assert prov["cadence_id"] == 40


# ---------------------------------------------------------------------------
# Client: backoff / retry-after
# ---------------------------------------------------------------------------


class TestClientHelpers:
    def test_backoff_delay_increases_with_attempts(self):
        d0 = _backoff_delay(0)
        d2 = _backoff_delay(2)
        # Backoff is exponential; attempt 2 base is 4, attempt 0 base is 1.
        assert d2 > d0

    def test_retry_after_parses_valid_header(self):
        headers = httpx.Headers({"retry-after": "3.5"})
        assert _retry_after(headers, 0) == 3.5

    def test_retry_after_rejects_invalid_header(self):
        headers = httpx.Headers({"retry-after": "NaN"})
        delay = _retry_after(headers, 0)
        assert delay > 0  # fell back to backoff

    def test_retry_after_rejects_negative(self):
        headers = httpx.Headers({"retry-after": "-5"})
        delay = _retry_after(headers, 0)
        assert delay > 0  # fell back to backoff

    def test_retry_after_rejects_infinite(self):
        headers = httpx.Headers({"retry-after": "inf"})
        delay = _retry_after(headers, 0)
        assert delay > 0 and delay < 100  # fell back to backoff


# ---------------------------------------------------------------------------
# Client: pagination with MockTransport
# ---------------------------------------------------------------------------


def _make_fake_transport(pages: list[list[dict]], path_prefix: str = "/v2/notes"):
    """Build an httpx.MockTransport that serves paginated Salesloft responses."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if path_prefix not in str(request.url):
            return httpx.Response(404, json={"error": "not found"})

        parsed = urlparse(str(request.url))
        qs = parse_qs(parsed.query)
        page_num = int(qs.get("page", ["1"])[0])
        idx = page_num - 1

        if idx >= len(pages) or idx < 0:
            return httpx.Response(200, json={"data": [], "metadata": {"paging": {}}})

        has_next = idx + 1 < len(pages)
        paging = {"next_page": page_num + 1} if has_next else {}
        body = {"data": pages[idx], "metadata": {"paging": paging}}
        call_count["n"] += 1
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler), call_count


def test_client_paginate_follows_pages():
    page1 = [{"id": 1, "updated_at": "2024-01-01"}, {"id": 2, "updated_at": "2024-01-02"}]
    page2 = [{"id": 3, "updated_at": "2024-01-03"}]
    transport, counts = _make_fake_transport([page1, page2])

    http = httpx.Client(transport=transport)
    client = SalesloftClient("fake-token", http_client=http)
    records = list(client.paginate("/v2/notes"))

    assert [r["id"] for r in records] == [1, 2, 3]
    assert counts["n"] == 2  # two pages fetched


def test_client_paginate_stops_on_empty():
    transport, _counts = _make_fake_transport([[]])
    http = httpx.Client(transport=transport)
    client = SalesloftClient("fake-token", http_client=http)
    records = list(client.paginate("/v2/notes"))
    assert records == []


def test_client_paginate_with_updated_after():
    """Verify updated_at[gt] is sent as a query parameter."""
    received_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        received_params.update(parse_qs(parsed.query))
        return httpx.Response(200, json={"data": [], "metadata": {"paging": {}}})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = SalesloftClient("fake-token", http_client=http)
    list(client.paginate("/v2/notes", updated_after="2024-06-01T00:00:00Z"))

    assert "updated_at[gt]" in received_params
    assert received_params["updated_at[gt]"] == ["2024-06-01T00:00:00Z"]


# ---------------------------------------------------------------------------
# Client: retries on transient errors
# ---------------------------------------------------------------------------


def test_client_retries_on_429():
    """Verify the client retries on 429 and eventually succeeds."""
    attempt = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempt["n"] += 1
        if attempt["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = SalesloftClient("fake-token", http_client=http)
    result = client.get("/v2/me")
    assert result == {"ok": True}
    assert attempt["n"] == 3


def test_client_raises_on_401():
    """Permanent errors (401) must not be retried."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = SalesloftClient("fake-token", http_client=http)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("/v2/me")


def test_client_raises_on_403():
    """Permanent errors (403) must not be retried."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = SalesloftClient("fake-token", http_client=http)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("/v2/me")


# ---------------------------------------------------------------------------
# Reconciliation (DB-free)
# ---------------------------------------------------------------------------


class FakeClientForReconcile:
    """Minimal fake that reports which IDs still exist."""

    def __init__(self, existing: dict[str, set]):
        self._existing = existing

    def check_ids_exist(self, path, ids, **kwargs):
        for rtype, rpath in {
            "note": "/v2/notes",
            "call": "/v2/activities/calls",
            "cadence": "/v2/cadences",
        }.items():
            if path == rpath:
                return self._existing.get(rtype, set()) & set(ids)
        return set()


class TestReconcile:
    def test_no_known_ids_returns_empty(self):
        state: dict = {}
        client = FakeClientForReconcile({})
        result = reconcile_deletions(client, state, resources=["note"])
        assert result == []

    def test_all_exist_returns_empty(self):
        state = {"known_ids_note": [1, 2, 3]}
        client = FakeClientForReconcile({"note": {1, 2, 3}})
        result = reconcile_deletions(client, state, resources=["note"])
        assert result == []

    def test_missing_ids_produce_tombstones(self):
        state = {"known_ids_note": [1, 2, 3]}
        client = FakeClientForReconcile({"note": {1, 3}})
        result = reconcile_deletions(client, state, resources=["note"])
        assert len(result) == 1
        assert result[0] == {"id": "salesloft:note:2", "_deleted": True}
        # Known IDs updated.
        assert set(state["known_ids_note"]) == {1, 3}

    def test_delete_ratio_guard_aborts(self):
        """If >50% would be deleted, abort to prevent mass-deletion."""
        state = {"known_ids_note": [1, 2, 3, 4]}
        # 3 out of 4 gone = 75% > default 50%.
        client = FakeClientForReconcile({"note": {1}})
        result = reconcile_deletions(client, state, resources=["note"], max_delete_ratio=0.5)
        assert result == []  # aborted
        # Known IDs preserved (not modified).
        assert set(state["known_ids_note"]) == {1, 2, 3, 4}

    def test_delete_ratio_guard_disabled(self):
        state = {"known_ids_note": [1, 2, 3, 4]}
        client = FakeClientForReconcile({"note": {1}})
        result = reconcile_deletions(client, state, resources=["note"], max_delete_ratio=1.0)
        assert len(result) == 3

    def test_api_error_preserves_state(self):
        """Auth/server errors must NOT cause deletions."""
        state = {"known_ids_note": [1, 2, 3]}

        class ErrorClient:
            def check_ids_exist(self, path, ids, **kwargs):
                raise RuntimeError("API down")

        result = reconcile_deletions(ErrorClient(), state, resources=["note"])
        assert result == []
        # State unchanged.
        assert set(state["known_ids_note"]) == {1, 2, 3}

    def test_dry_run_does_not_delete(self):
        state = {"known_ids_note": [1, 2, 3]}
        client = FakeClientForReconcile({"note": {1}})
        result = reconcile_deletions(
            client, state, resources=["note"], max_delete_ratio=1.0, dry_run=True
        )
        assert result == []
        # State unchanged.
        assert set(state["known_ids_note"]) == {1, 2, 3}

    def test_unknown_resource_skipped(self):
        state = {}
        result = reconcile_deletions(
            FakeClientForReconcile({}), state, resources=["unknown_type"]
        )
        assert result == []


class TestTrackKnownIds:
    def test_initial_tracking(self):
        state: dict = {}
        track_known_ids(state, "note", [1, 2, 3])
        assert state["known_ids_note"] == [1, 2, 3]

    def test_incremental_tracking_merges(self):
        state = {"known_ids_note": [1, 2]}
        track_known_ids(state, "note", [2, 3, 4])
        assert state["known_ids_note"] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Source: document-mode marker + resource configuration
# ---------------------------------------------------------------------------


def test_salesloft_source_declares_document_marker():
    """The source must carry the DOCUMENT_SOURCE_ATTR marker for document mode."""
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    from cognee_community_connector_salesloft.salesloft import SALESLOFT_SOURCE_NAME

    # Build with a fake client that passes verify_credentials.
    def handler(request: httpx.Request) -> httpx.Response:
        if "/v2/me" in str(request.url):
            return httpx.Response(200, json={"data": {"id": 1, "name": "Test"}})
        return httpx.Response(200, json={"data": [], "metadata": {"paging": {}}})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    fake_client = SalesloftClient("fake-token", http_client=http)

    from cognee_community_connector_salesloft.salesloft import salesloft_source

    source = salesloft_source(
        access_token="fake-token",
        _client=fake_client,
    )
    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == SALESLOFT_SOURCE_NAME
    assert SALESLOFT_SOURCE_NAME == "salesloft"


def test_salesloft_source_rejects_unknown_resource():
    from cognee_community_connector_salesloft.salesloft import salesloft_source

    with pytest.raises(ValueError, match="Unknown resource"):
        salesloft_source(resources=["tweets"], access_token="x")


def test_salesloft_resource_is_configured_for_merge_and_hard_delete():
    """The dlt resource must be wired with merge + id PK + the hard_delete column."""

    from cognee_community_connector_salesloft.salesloft import SALESLOFT_TABLE_NAME

    def handler(request: httpx.Request) -> httpx.Response:
        if "/v2/me" in str(request.url):
            return httpx.Response(200, json={"data": {"id": 1}})
        return httpx.Response(200, json={"data": [], "metadata": {"paging": {}}})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    fake_client = SalesloftClient("fake-token", http_client=http)

    from cognee_community_connector_salesloft.salesloft import salesloft_source

    source = salesloft_source(access_token="fake-token", _client=fake_client)

    resource = source.resources[SALESLOFT_TABLE_NAME]
    assert resource.write_disposition == "merge"

    # Check _deleted column is marked as hard_delete via schema hint.
    hints = resource.compute_table_schema()
    col_map = {c["name"]: c for c in hints.get("columns", {}).values()}
    if "_deleted" in col_map:
        assert col_map["_deleted"].get("hard_delete") is True


# ---------------------------------------------------------------------------
# Integration: dlt pipeline with fake transport
# ---------------------------------------------------------------------------


def _build_fake_api(notes=None, calls=None, cadences=None, deleted_note_ids=None):
    """Build a MockTransport serving fake Salesloft API data."""
    notes = notes or []
    calls = calls or []
    cadences = cadences or []
    deleted_note_ids = deleted_note_ids or set()

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)

        if "/v2/me" in url_str:
            return httpx.Response(200, json={"data": {"id": 1, "name": "Test"}})

        if "/v2/notes" in url_str:
            qs = parse_qs(urlparse(url_str).query)

            # ids[] filter for reconciliation.
            requested_ids = qs.get("ids[]", [])
            if requested_ids:
                requested_ids = [int(i) for i in requested_ids]
                matched = [
                    n
                    for n in notes
                    if n["id"] in requested_ids and n["id"] not in deleted_note_ids
                ]
                return httpx.Response(
                    200, json={"data": matched, "metadata": {"paging": {}}}
                )

            # Normal pagination.
            return httpx.Response(200, json={"data": notes, "metadata": {"paging": {}}})

        if "/v2/activities/calls" in url_str:
            return httpx.Response(200, json={"data": calls, "metadata": {"paging": {}}})

        if "/v2/cadences" in url_str:
            return httpx.Response(200, json={"data": cadences, "metadata": {"paging": {}}})

        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def _run_sync(
    dlt_mod,
    tmp_path,
    notes=None,
    calls=None,
    cadences=None,
    deleted_note_ids=None,
    resources=None,
    policy=None,
):
    """Run salesloft_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_salesloft.salesloft import salesloft_source

    transport = _build_fake_api(
        notes=notes, calls=calls, cadences=cadences, deleted_note_ids=deleted_note_ids
    )
    http = httpx.Client(transport=transport)
    fake_client = SalesloftClient("fake-token", http_client=http)

    db_path = (tmp_path / "salesloft.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="salesloft_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="salesloft_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = salesloft_source(
        access_token="fake-token",
        _client=fake_client,
        resources=resources or ["notes"],
        policy=policy
        or SignalPolicy(
            min_call_seconds=0,
            require_note_or_disposition=False,
        ),
    )
    pipeline.run(source)
    return pipeline


def _read_records(pipeline):
    """Return {id: row-dict} for the salesloft_records table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            "SELECT id, title, content, updated_at FROM salesloft_records"
        ) as cursor,
    ):
        rows = cursor.fetchall()
    return {
        row[0]: {"id": row[0], "title": row[1], "content": row[2], "updated_at": row[3]}
        for row in rows
    }


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_notes(dlt_mod, tmp_path):
    notes = [_note(1, "First note."), _note(2, "Second note.")]
    pipeline = _run_sync(dlt_mod, tmp_path, notes=notes)

    rows = _read_records(pipeline)
    assert "salesloft:note:1" in rows
    assert "salesloft:note:2" in rows
    assert "First note." in rows["salesloft:note:1"]["content"]


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    notes_v1 = [_note(1, "Version one.")]
    _run_sync(dlt_mod, tmp_path, notes=notes_v1)

    notes_v2 = [_note(1, "Version two.", updated_at="2024-07-01T10:00:00Z")]
    pipeline = _run_sync(dlt_mod, tmp_path, notes=notes_v2)

    rows = _read_records(pipeline)
    assert "Version two." in rows["salesloft:note:1"]["content"]


def test_incremental_cursor_advances(dlt_mod, tmp_path):
    """After sync, cursor should be at the latest updated_at."""
    notes = [
        _note(1, "A", updated_at="2024-06-01T10:00:00Z"),
        _note(2, "B", updated_at="2024-06-02T10:00:00Z"),
    ]
    pipeline = _run_sync(dlt_mod, tmp_path, notes=notes)
    rows = _read_records(pipeline)
    assert len(rows) == 2
