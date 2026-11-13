"""Tests for the Deel dlt connector.

All HTTP is served by ``FakeDeel`` (see ``conftest.py``) through the real dlt retrying
session and RESTClient, and loads go to a temporary sqlite destination — no network and
no credentials. Fixtures under ``fixtures/`` are obviously fake.
"""

import copy
import logging
from types import SimpleNamespace

import pytest
from conftest import BASE_URL, FAKE_TOKEN, ids, read_table

from cognee_community_connector_deel import (
    DeelAuthError,
    DeelDeleteGuardError,
    deel_source,
)
from cognee_community_connector_deel import deel as deel_module
from cognee_community_connector_deel.deel import (
    DEFAULT_DOCUMENT_TYPES,
    DEFAULT_DOCUMENTS_PATH,
    _Api,
    _Config,
    _Skips,
    _sync,
)

CONTRACT_IDS = {f"deel:contract:c{i}" for i in range(1, 6)}
PEOPLE_IDS = {f"deel:person:p{i}" for i in range(1, 4)}
RUN = {"write_disposition": "merge", "primary_key": "id"}

CONFIG_DEFAULTS = {
    "resources": ("contracts", "people"),
    "contract_statuses": (),
    "contract_types": (),
    "include_pii": False,
    "include_contract_documents": False,
    "document_max_bytes": 5_000_000,
    "document_types": DEFAULT_DOCUMENT_TYPES,
    "documents_path": DEFAULT_DOCUMENTS_PATH,
    "page_size": 2,
    "overlap_seconds": 3600,
    "full_reconcile": False,
    "max_delete_ratio": 0.5,
    "force_delete": False,
    "drop_statuses": (),
    "skipped_table": False,
}


def cfg(**overrides):
    return _Config(**(CONFIG_DEFAULTS | overrides))


@pytest.fixture
def sync(session_for):
    """Run ``_sync`` directly against a FakeDeel with an in-memory state dict."""

    def run(kind, fake, state, disposition="merge", now=1_000, skips=None, **overrides):
        api = _Api(BASE_URL, FAKE_TOKEN, session_for(fake))
        return list(_sync(kind, api, cfg(**overrides), state, disposition, skips or _Skips(), now))

    return run


def causes(exc):
    """Walk an exception's cause/context chain (dlt wraps source errors)."""
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def fail_with(exc_type, call):
    with pytest.raises(Exception) as info:
        call()
    matches = [e for e in causes(info.value) if isinstance(e, exc_type)]
    assert matches, (
        f"expected {exc_type.__name__} in {[type(e).__name__ for e in causes(info.value)]}"
    )
    return matches[0]


def known_state(pipeline, table="deel_contracts"):
    return pipeline.state["sources"]["deel"]["resources"][table]


# ---------------------------------------------------------------------------
# 1. Ingest path
# ---------------------------------------------------------------------------


def test_multi_page_pagination_ingest(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, page_size=2), **RUN)

    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS
    assert ids(pipeline, "deel_people") == PEOPLE_IDS
    # 5 contracts at 2 per page = 3 pages (+1 startup probe); 3 people = 2 pages (+1 probe).
    assert fake.count("/contracts") == 4
    assert fake.count("/people") == 3
    assert {call[2] for call in fake.calls} == {f"Bearer {FAKE_TOKEN}"}


def test_source_is_a_document_source(fake, make_source, pipeline_factory):
    from cognee.tasks.ingestion.dlt_utils import document_source_tag
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    source = make_source(fake)
    assert document_source_tag(source) == "deel"

    pipeline = pipeline_factory()
    pipeline.run(source, **RUN)
    (row,) = read_table(pipeline, "deel_contracts", "id, title, content")[:1]
    item = _build_document_data_item(
        SimpleNamespace(
            row_data={"id": row[0], "title": row[1], "content": row[2]}, content_hash="h"
        ),
        data_id="00000000-0000-0000-0000-000000000000",
        source_tag="deel",
    )
    assert item.external_metadata["source"] == "deel"
    assert item.external_metadata["external_id"].startswith("deel:contract:")
    assert item.data.startswith("# Deel contract:")


# ---------------------------------------------------------------------------
# 2-3. Incremental cursor
# ---------------------------------------------------------------------------


def test_incremental_cursor_second_run_only_changes(fake, sync):
    state = {}
    first = sync("contracts", fake, state)
    assert {r["source_id"] for r in first} == {f"c{i}" for i in range(1, 6)}

    fake.contracts[1]["title"] = "Fake Role 2 (promoted)"
    fake.contracts.append({**copy.deepcopy(fake.contracts[0]), "id": "c6", "title": "New"})
    second = sync("contracts", fake, state, now=2_000)

    assert {r["source_id"] for r in second if not r["_deleted"]} == {"c2", "c6"}
    assert state["last_run"]["emitted"] == 2
    assert not [r for r in second if r["_deleted"]]
    # first_seen is preserved for unchanged records and advanced only for changed ones.
    assert state["known"]["c1"].endswith(":1000")
    assert state["known"]["c2"].endswith(":2000")


def test_people_cursor_and_overlap_window(fake, sync):
    # updated_at: p1=02-01, p2=02-02, p3=02-03 (cursor after run 1).
    state = {}
    sync("people", fake, state)
    assert state["cursor"].startswith("2025-02-03")

    # Unchanged data, 1-day overlap: records at/after cursor-1d (p2 at exactly the edge, p3).
    rows = sync("people", fake, state, overlap_seconds=86_400)
    assert {r["source_id"] for r in rows} == {"p2", "p3"}
    # No overlap beyond the boundary: p1 (2 days old) is not re-sent.
    assert "p1" not in {r["source_id"] for r in rows}
    # A tiny overlap re-sends only the newest record.
    assert {r["source_id"] for r in sync("people", fake, state, overlap_seconds=60)} == {"p3"}


def test_cursor_not_advanced_on_failed_extract(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)
    before = copy.deepcopy(known_state(pipeline))

    fake.contracts[0]["title"] = "changed"
    fake.fail_from_call["/contracts"] = (fake.count("/contracts") + 3, 403)  # revoked mid-pass
    fail_with(DeelAuthError, lambda: pipeline_factory().run(make_source(fake, page_size=2), **RUN))

    assert known_state(pipeline_factory()) == before


def test_cursor_not_advanced_on_failed_load(fake, make_source, pipeline_factory, monkeypatch):
    from dlt.pipeline.pipeline import Pipeline

    real_load = Pipeline.load

    def failing_load(self, *args, **kwargs):
        raise RuntimeError("destination unavailable")

    pipeline = pipeline_factory()
    monkeypatch.setattr(Pipeline, "load", failing_load)
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the failure
        pipeline.run(make_source(fake), **RUN)
    monkeypatch.setattr(Pipeline, "load", real_load)

    # Nothing was loaded, so a resumed run must still deliver every record (no skipped rows).
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)
    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS
    assert ids(pipeline, "deel_people") == PEOPLE_IDS


# ---------------------------------------------------------------------------
# 4-8. Deletes
# ---------------------------------------------------------------------------


def test_tombstone_from_single_pass_for_missing_contract_id(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)

    del fake.contracts[2]  # c3 deleted upstream
    calls_before = fake.count("/contracts")
    pipeline.run(make_source(fake, page_size=2), **RUN)

    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS - {"deel:contract:c3"}
    # Emission and the delete sweep share one pass: 1 probe... 3 pages, not 2 sweeps.
    assert fake.count("/contracts") - calls_before == 3
    assert known_state(pipeline)["last_run"]["tombstoned"] == 1


@pytest.mark.parametrize("status", [429, 500])
def test_partial_sweep_emits_no_deletes(fake, make_source, pipeline_factory, status):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, page_size=2), **RUN)

    # Page 2 never succeeds, so c2..c5 are unseen: they must not be mistaken for deletions.
    fake.fail_from_call["/contracts"] = (fake.count("/contracts") + 3, status)  # page 2 never works
    pipeline.run(make_source(fake, page_size=1), **RUN)

    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS
    run = known_state(pipeline)["last_run"]
    assert run["sweep"] == f"incomplete:http_{status}" and run["tombstoned"] == 0
    # Unseen ids stay known, so a later clean pass can still delete them.
    assert set(known_state(pipeline)["known"]) == {f"c{i}" for i in range(1, 6)}


def test_truncated_or_looping_pages_are_incomplete(fake, sync):
    state = {}
    sync("contracts", fake, state)

    fake.total_override["/contracts"] = 99  # advertises more rows than it returned
    sync("contracts", fake, state)
    assert state["last_run"]["sweep"] == "incomplete:truncated"

    fake.total_override.clear()
    fake.repeat_cursor = True  # server keeps handing back the same cursor
    sync("contracts", fake, state)
    assert state["last_run"]["sweep"].startswith("incomplete:")
    assert len(state["known"]) == 5


def test_scope_loss_empty_list_does_not_wipe_graph(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)

    fake.contracts, fake.people = [], []  # token lost its scope: 200 with an empty list
    pipeline.run(make_source(fake), **RUN)

    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS
    assert ids(pipeline, "deel_people") == PEOPLE_IDS
    assert known_state(pipeline)["last_run"]["sweep"] == "incomplete:empty_response"


def test_max_delete_ratio_aborts_and_force_overrides(fake, make_source, pipeline_factory, sync):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)

    fake.contracts = fake.contracts[:2]  # 3 of 5 = 60% > 50%
    guard = fail_with(
        DeelDeleteGuardError, lambda: pipeline_factory().run(make_source(fake), **RUN)
    )
    assert "force_delete=True" in str(guard)
    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS  # nothing deleted

    pipeline_factory().run(make_source(fake, force_delete=True), **RUN)
    assert ids(pipeline, "deel_contracts") == {"deel:contract:c1", "deel:contract:c2"}


def test_terminated_contract_stays_unless_in_drop_statuses(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)

    fake.contracts[1]["status"] = "cancelled"
    pipeline.run(make_source(fake), **RUN)
    statuses = dict(read_table(pipeline, "deel_contracts", "id, status"))
    assert statuses["deel:contract:c2"] == "cancelled"  # stays as a status update
    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS

    pipeline.run(make_source(fake, drop_statuses=["cancelled"]), **RUN)
    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS - {"deel:contract:c2"}


# ---------------------------------------------------------------------------
# 9-10. Contract documents (opt-in)
# ---------------------------------------------------------------------------


def _document_fixture(fake):
    ok = "https://deel.example.test/files/ok.txt"
    big = "https://deel.example.test/files/big.txt"
    wrong_type = "https://deel.example.test/files/pic.png"
    foreign = "https://storage.example.test/files/other.txt"
    fake.documents["c1"] = [
        {"id": "d-ok", "download_url": ok},
        {"id": "d-big", "download_url": big},
        {"id": "d-img", "download_url": wrong_type},
        {"id": "d-nourl", "document_type": "FRAMEWORK_AGREEMENT"},
        {"id": "d-foreign", "download_url": foreign},
    ]
    fake.files["/files/ok.txt"] = (200, "text/plain; charset=utf-8", b"Fake clause one.", {})
    fake.files["/files/big.txt"] = (200, "text/plain", b"x" * 50, {"content-length": "50"})
    fake.files["/files/pic.png"] = (200, "image/png", b"\x89PNG", {})
    fake.files["/files/other.txt"] = (200, "text/plain", b"Fake foreign text.", {})


def test_document_endpoint_not_called_when_opt_in_off(fake, make_source, pipeline_factory):
    _document_fixture(fake)
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)

    assert not [p for p in fake.paths() if "/documents" in p or p.startswith("/files")]
    assert "deel_contract_documents" not in pipeline.default_schema.tables


def test_contract_documents_opt_in_with_size_cap_and_type_allowlist_and_skip_reason(
    fake, make_source, pipeline_factory
):
    _document_fixture(fake)
    pipeline = pipeline_factory()
    source = make_source(
        fake,
        resources=["contracts"],
        include_contract_documents=True,
        document_max_bytes=20,
        skipped_table=True,
    )
    pipeline.run(source, **RUN)

    docs = read_table(pipeline, "deel_contract_documents", "id, content")
    assert {d[0] for d in docs} == {
        "deel:contract-document:c1:d-ok",
        "deel:contract-document:c1:d-foreign",
    }
    assert any("Fake clause one." in d[1] for d in docs)
    skipped = {row[0]: row[1] for row in read_table(pipeline, "deel_skipped", "source_id, reason")}
    assert skipped == {
        "c1:d-big": "too_large",
        "c1:d-img": "type_not_allowed",
        "c1:d-nourl": "no_download_url",
    }
    # The bearer token goes to the Deel host only, never to other hosts.
    auth_by_path = {path: auth for path, _, auth in fake.calls}
    assert auth_by_path["/files/ok.txt"] == f"Bearer {FAKE_TOKEN}"
    assert auth_by_path["/files/other.txt"] is None

    # Deleting the contract upstream forgets its documents too.
    fake.contracts = [c for c in fake.contracts if c["id"] != "c1"]
    pipeline.run(make_source(fake, resources=["contracts"], include_contract_documents=True), **RUN)
    assert read_table(pipeline, "deel_contract_documents", "id") == []


def test_documents_require_contracts_resource():
    with pytest.raises(ValueError, match="contracts"):
        deel_source(token=FAKE_TOKEN, resources=["people"], include_contract_documents=True)


# ---------------------------------------------------------------------------
# 11-13. Privacy and change detection
# ---------------------------------------------------------------------------

SENSITIVE_NEVER = ["99999", "FAKE-SIG", "must-not-leak", "1990-01-01", "000-00-0000", "boss@"]
PII = [
    "worker1@example.invalid",
    "Fake Worker 1",
    "p1@example.invalid",
    "Fake1 Person",
    "1 Fake St",
]


def _everything(pipeline):
    text = []
    for table in ("deel_contracts", "deel_people"):
        for row in read_table(pipeline, table, "*"):
            text.extend(str(v) for v in row)
    return "\n".join(text)


def test_field_allowlist_drops_unknown_attributes_and_pii_by_default(
    fake, make_source, pipeline_factory
):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)
    blob = _everything(pipeline)
    for secret in SENSITIVE_NEVER + PII:
        assert secret not in blob, secret
    for kept in ("Fake Role 1", "Fake Team", "Senior", "Platform", "Engineer 1"):
        assert kept in blob


def test_include_pii_opt_in(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, include_pii=True), **RUN)
    blob = _everything(pipeline)
    for pii in PII:
        assert pii in blob, pii
    # Even with the opt-in, compensation, ids/birth dates and unknown fields never flow through.
    for secret in SENSITIVE_NEVER:
        assert secret not in blob, secret


def test_people_hash_skip_unchanged_workers(fake, sync):
    for person in fake.people:
        person["updated_at"] = None  # no usable updated_at: hash is the only signal
    state = {}
    assert len(sync("people", fake, state)) == 3
    assert sync("people", fake, state) == []

    fake.people[1]["job_title"] = "Staff Engineer"
    rows = sync("people", fake, state)
    assert [r["source_id"] for r in rows] == ["p2"]

    del fake.people[0]
    rows = sync("people", fake, state)
    assert [(r["source_id"], r["_deleted"]) for r in rows] == [("p1", True)]


# ---------------------------------------------------------------------------
# 14-15. Resilience
# ---------------------------------------------------------------------------


def test_429_retry_after_honoured_and_5xx_backoff(fake, session_for, monkeypatch):
    delays = []
    monkeypatch.setattr("tenacity.nap.time.sleep", delays.append)
    fake.scripted["/contracts"] = [(429, {"Retry-After": "3"}), (503, {}), (503, {})]
    api = _Api(BASE_URL, FAKE_TOKEN, session_for(fake, max_attempts=5, backoff_factor=1.0))

    assert api.probe("/contracts") == 200
    assert delays[0] == 3  # Retry-After wins over the computed backoff
    assert 0 < delays[1] < delays[2]  # exponential backoff for 5xx without Retry-After


def test_bad_record_skipped_with_reason_and_sync_continues(
    fake, make_source, pipeline_factory, caplog
):
    bad = [
        {"title": "no id at all"},
        {"id": "c-bad", "title": {"nested": "object"}},
        "not-an-object",
    ]
    fake.contracts = fake.contracts[:2] + bad + fake.contracts[2:]
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, skipped_table=True), **RUN)

    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS
    skipped = read_table(pipeline, "deel_skipped", "source_id, reason")
    assert sorted(skipped, key=str) == sorted(
        [("c-bad", "unexpected_type:title"), (None, "missing_id"), (None, "not_an_object")],
        key=str,
    )
    # A record we could not identify might be a known one: no deletions on that pass.
    assert known_state(pipeline)["last_run"]["sweep"] == "incomplete:record_without_id"
    assert known_state(pipeline)["last_run"]["skipped"] == 3
    # Each skip is logged with its id and reason (never the record's content).
    assert "skipped record c-bad (unexpected_type:title)" in caplog.text
    assert "no id at all" not in caplog.text and "nested" not in caplog.text


# ---------------------------------------------------------------------------
# 16. Startup validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "needle"), [(401, "401"), (403, "contracts:read")])
def test_startup_validation_fails_fast_with_clear_message(
    fake, make_source, pipeline_factory, status, needle
):
    fake.status_for["/contracts"] = status
    error = fail_with(DeelAuthError, lambda: pipeline_factory().run(make_source(fake), **RUN))

    assert needle in str(error)
    assert FAKE_TOKEN not in str(error)
    assert fake.paths() == ["/contracts"]  # one probe, no sync work


def test_token_required_and_sources(monkeypatch):
    monkeypatch.delenv("DEEL_API_TOKEN", raising=False)
    monkeypatch.setattr(deel_module, "_resolve_token", deel_module._resolve_token)
    with pytest.raises(ValueError, match="DEEL_API_TOKEN"):
        deel_source(base_url=BASE_URL)
    monkeypatch.setenv("DEEL_API_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("DEEL_BASE_URL", "https://deel.example.test/rest/")
    assert deel_source() is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resources": ["invoices"]},
        {"resources": []},
        {"max_delete_ratio": 0},
        {"max_delete_ratio": 1.5},
        {"page_size": 0},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        deel_source(token=FAKE_TOKEN, **kwargs)


# ---------------------------------------------------------------------------
# 17. Logs
# ---------------------------------------------------------------------------


def test_no_token_or_content_in_captured_logs(
    fake, make_source, pipeline_factory, caplog, capfd, monkeypatch
):
    recorded = []
    recorder = SimpleNamespace(
        **{
            level: (lambda msg, *args, **kw: recorded.append(str(msg) % args if args else str(msg)))
            for level in ("debug", "info", "warning", "error", "exception")
        }
    )
    monkeypatch.setattr(deel_module, "logger", recorder)
    fake.contracts.append({"title": "no id"})  # exercise the skip path as well
    caplog.set_level(logging.DEBUG)

    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, include_pii=True, page_size=500), **RUN)
    del fake.contracts[0]
    pipeline.run(make_source(fake, include_pii=True), **RUN)

    out, err = capfd.readouterr()
    haystack = "\n".join(recorded) + caplog.text + out + err
    assert "sweep=" in "\n".join(recorded)  # the run summary is logged
    for forbidden in [FAKE_TOKEN, "Fake Role", "Fake Worker", "example.invalid", "Fake Team"]:
        assert forbidden not in haystack, forbidden


# ---------------------------------------------------------------------------
# 18. Write disposition and full_reconcile
# ---------------------------------------------------------------------------


def test_resources_declare_merge_and_hard_delete(fake, make_source):
    source = make_source(fake)
    for name in ("deel_contracts", "deel_people"):
        resource = source.resources[name]
        assert resource.write_disposition == "merge"
        assert resource.columns["_deleted"]["hard_delete"] is True
        assert resource.compute_table_schema()["columns"]["id"]["primary_key"] is True


def test_replace_override_falls_back_to_full_snapshot_instead_of_wiping(
    fake, make_source, pipeline_factory
):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)

    # cognee's default is write_disposition="replace": an incremental emit would drop
    # every unchanged record from staging, and orphan_cleanup would then purge them.
    pipeline.run(make_source(fake), write_disposition="replace", primary_key="id")

    assert ids(pipeline, "deel_contracts") == CONTRACT_IDS
    assert ids(pipeline, "deel_people") == PEOPLE_IDS


def test_full_reconcile_and_chosen_write_disposition_behaviour(fake, sync):
    state = {}
    sync("contracts", fake, state)
    assert list(sync("contracts", fake, state)) == []  # incremental: nothing changed

    del fake.contracts[0]
    rows = sync("contracts", fake, state, full_reconcile=True)
    live = [r for r in rows if not r["_deleted"]]
    assert len(live) == 4  # everything re-sent, changed or not
    assert [r["source_id"] for r in rows if r["_deleted"]] == ["c1"]  # sweep still deletes
    assert state["last_run"]["mode"] == "snapshot"

    # Under replace there are no tombstones: absence from the snapshot is the delete signal.
    del fake.contracts[0]
    rows = sync("contracts", fake, {}, disposition="replace")
    assert len(rows) == 3 and not any(r["_deleted"] for r in rows)


def test_contracts_leaving_a_filter_are_tombstoned(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)

    # c1, c3, c5 are "ongoing_time_based", c2, c4 are "eor": narrowing the filter makes two vanish.
    pipeline.run(
        make_source(
            fake, resources=["contracts"], contract_types=["ongoing_time_based"], force_delete=True
        ),
        **RUN,
    )

    assert ids(pipeline, "deel_contracts") == {f"deel:contract:c{i}" for i in (1, 3, 5)}
    assert known_state(pipeline)["last_run"]["tombstoned"] == 2
    assert ids(pipeline, "deel_people") == PEOPLE_IDS  # other resources are untouched


def test_contract_status_and_type_filters_are_sent(fake, sync):
    sync("contracts", fake, {}, contract_statuses=["in_progress"], contract_types=["eor"])
    params = next(p for path, p, _ in fake.calls if path == "/contracts")
    assert params["statuses"] == "in_progress" and params["types"] == "eor"


def test_page_size_is_capped_at_documented_maximum(fake, make_source, pipeline_factory):
    pipeline_factory().run(make_source(fake, page_size=5000), **RUN)
    limits = {params["limit"] for path, params, _ in fake.calls if path == "/contracts"}
    assert limits == {"1", "100"}  # the startup probe, then the capped page size


# ---------------------------------------------------------------------------
# Document text extraction
# ---------------------------------------------------------------------------


def minimal_pdf(text):
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode()
    objs[3] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offsets:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_pdf_text_extraction_and_skip_reasons():
    from cognee_community_connector_deel.deel import _extract_text

    assert _extract_text("application/pdf", minimal_pdf("Fake clause seven")) == (
        None,
        "Fake clause seven",
    )
    assert _extract_text("application/pdf", b"not a pdf") == ("unparseable", None)
    assert _extract_text("text/plain", b"   ") == ("empty", None)
