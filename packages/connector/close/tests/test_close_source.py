"""Unit tests for the Close CRM connector.

Uses httpx.MockTransport to fake the Close API — no live credentials needed.

Two layers:
* DB-free tests for rendering, hashing, pagination, retry, and the sync state
  machine (_iter_rows).
* dlt-pipeline tests (mocked Close API, temp sqlite destination) covering the
  acceptance criteria: first sync loads, edits are reflected, and deleted objects
  are removed via hard-delete tombstones (forget-on-delete).
"""

import httpx
import pytest

from cognee_community_connector_close.close import (
    ALL_TYPES,
    CLOSE_BASE_URL,
    CLOSE_SOURCE_NAME,
    DEFAULT_TYPES,
    OBJECT_PATHS,
    _CloseClient,
    _build_lead_lookup,
    _content_hash,
    _iter_rows,
    _normalize_types,
    _object_to_row,
    _render_call,
    _render_email,
    _render_lead,
    _render_note,
    _render_opportunity,
    _strip_html,
    _truncate,
    close_source,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _lead(lead_id="lead_abc", name="Acme Corp", **kwargs):
    """Create a minimal Close lead fixture."""
    base = {
        "id": lead_id,
        "display_name": name,
        "description": "",
        "url": "",
        "status_label": "Potential",
        "contacts": [],
        "date_updated": "2026-01-01T00:00:00Z",
    }
    base.update(kwargs)
    return base


def _opportunity(opp_id="oppo_abc", lead_id="lead_abc", **kwargs):
    base = {
        "id": opp_id,
        "lead_id": lead_id,
        "value": 5000,
        "value_formatted": "$5,000",
        "status_label": "Active",
        "confidence": 50,
        "note": "",
        "date_won": None,
        "date_updated": "2026-01-01T00:00:00Z",
    }
    base.update(kwargs)
    return base


def _note(note_id="acti_note_abc", lead_id="lead_abc", **kwargs):
    base = {
        "id": note_id,
        "lead_id": lead_id,
        "note": "Follow up on proposal.",
        "note_html": None,
        "date_created": "2026-01-15T10:00:00Z",
        "date_updated": "2026-01-15T10:00:00Z",
    }
    base.update(kwargs)
    return base


def _call(call_id="acti_call_abc", lead_id="lead_abc", **kwargs):
    base = {
        "id": call_id,
        "lead_id": lead_id,
        "direction": "outbound",
        "disposition": "answered",
        "duration": 1680,
        "summary": "Discussed migration timeline.",
        "note": "",
        "recording_transcript": None,
        "voicemail_transcript": None,
        "date_created": "2026-01-20T14:00:00Z",
        "date_updated": "2026-01-20T14:00:00Z",
    }
    base.update(kwargs)
    return base


def _email(email_id="acti_emai_abc", lead_id="lead_abc", **kwargs):
    base = {
        "id": email_id,
        "lead_id": lead_id,
        "subject": "Proposal follow-up",
        "direction": "outgoing",
        "sender": "me@example.com",
        "to": ["alice@acme.com"],
        "body_text": "Hi Alice, here is the proposal.",
        "body_html": None,
        "date_created": "2026-02-01T09:00:00Z",
        "date_updated": "2026-02-01T09:00:00Z",
    }
    base.update(kwargs)
    return base


class _FakeCloseAPI:
    """In-memory Close API that backs an httpx.MockTransport.

    Add objects to the ``leads``, ``opportunities``, ``notes``, ``calls``, and
    ``emails`` dicts.  The handler serves paginated list responses and handles
    ``_fields`` filtering just like the real Close API.
    """

    def __init__(self):
        self.leads: dict[str, dict] = {}
        self.opportunities: dict[str, dict] = {}
        self.notes: dict[str, dict] = {}
        self.calls: dict[str, dict] = {}
        self.emails: dict[str, dict] = {}
        self.me: dict = {"organization_id": "org_test123"}
        # Set to an HTTP status to simulate transient errors.
        self.next_error: int | None = None
        self.error_count: int = 0

    def _collections(self) -> dict[str, dict]:
        return {
            "/api/v1/lead/": self.leads,
            "/api/v1/opportunity/": self.opportunities,
            "/api/v1/activity/note/": self.notes,
            "/api/v1/activity/call/": self.calls,
            "/api/v1/activity/email/": self.emails,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        """httpx.MockTransport handler — routes by URL path."""
        if self.next_error and self.error_count > 0:
            self.error_count -= 1
            return httpx.Response(self.next_error, json={"error": "simulated"})

        path = request.url.path

        if path == "/api/v1/me/":
            return httpx.Response(200, json=self.me)

        collections = self._collections()
        if path in collections:
            return self._list_response(collections[path], request)

        return httpx.Response(404, json={"error": "not found"})

    def _list_response(self, objects: dict, request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params.get("_skip", "0"))
        limit = int(request.url.params.get("_limit", "100"))
        items = list(objects.values())
        page = items[skip : skip + limit]

        # Honor _fields filtering like the real Close API.
        fields_param = request.url.params.get("_fields")
        if fields_param:
            field_list = [f.strip() for f in fields_param.split(",")]
            page = [{k: obj[k] for k in field_list if k in obj} for obj in page]

        return httpx.Response(
            200,
            json={"data": page, "has_more": skip + limit < len(items)},
        )


def _make_client(fake: _FakeCloseAPI) -> _CloseClient:
    """Build a _CloseClient backed by the in-memory fake."""
    transport = httpx.MockTransport(fake.handler)
    http = httpx.Client(transport=transport)
    return _CloseClient(http, "test-api-key")


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


class TestRenderLead:
    def test_basic(self):
        rendered = _render_lead(_lead(name="Acme Corp", status_label="Active"))
        assert "Lead: Acme Corp" in rendered
        assert "Status: Active" in rendered

    def test_with_contacts(self):
        lead = _lead(
            contacts=[
                {
                    "display_name": "Alice",
                    "title": "CTO",
                    "emails": [{"email": "alice@acme.com"}],
                    "phones": [{"phone": "+1234567890"}],
                }
            ]
        )
        rendered = _render_lead(lead)
        assert "Contact: Alice, CTO" in rendered
        assert "Email: alice@acme.com" in rendered
        assert "Phone: +1234567890" in rendered

    def test_with_description_and_website(self):
        rendered = _render_lead(
            _lead(description="Enterprise SaaS company", url="https://acme.com")
        )
        assert "Description: Enterprise SaaS company" in rendered
        assert "Website: https://acme.com" in rendered


class TestRenderOpportunity:
    def test_basic(self):
        rendered = _render_opportunity(
            _opportunity(value_formatted="$10,000", confidence=75), "Acme Corp"
        )
        assert "Opportunity: $10,000" in rendered
        assert "Lead: Acme Corp" in rendered
        assert "Confidence: 75%" in rendered


class TestRenderNote:
    def test_plain_text(self):
        rendered = _render_note(_note(note="Follow up on proposal."), "Acme Corp")
        assert "Note on lead: Acme Corp" in rendered
        assert "Follow up on proposal." in rendered
        assert "Date: 2026-01-15" in rendered

    def test_html_is_stripped(self):
        rendered = _render_note(
            _note(note_html="<p>Bold <b>text</b></p><br/>Next line"),
            "Acme Corp",
        )
        assert "Bold text" in rendered
        assert "<p>" not in rendered
        assert "<b>" not in rendered


class TestRenderCall:
    def test_with_summary(self):
        rendered = _render_call(
            _call(summary="Discussed migration timeline.", duration=1680),
            "Acme Corp",
        )
        assert "Call (outbound, answered, 28 min)" in rendered
        assert "Lead: Acme Corp" in rendered
        assert "Summary: Discussed migration timeline." in rendered

    def test_without_summary_or_transcript(self):
        rendered = _render_call(
            _call(summary=None, recording_transcript=None, duration=0),
            "Acme Corp",
        )
        assert "Lead: Acme Corp" in rendered
        assert "Summary" not in rendered
        assert "Transcript" not in rendered

    def test_with_transcript(self):
        rendered = _render_call(
            _call(recording_transcript="Agent: Hello.\nCustomer: Hi there."),
            "Acme Corp",
        )
        assert "Transcript:" in rendered
        assert "Agent: Hello." in rendered

    def test_truncates_long_transcript(self):
        rendered = _render_call(
            _call(recording_transcript="x" * 20_000), "Acme Corp"
        )
        assert "[truncated]" in rendered
        assert len(rendered) < 20_000

    def test_with_voicemail(self):
        rendered = _render_call(
            _call(voicemail_transcript="Hi, please call me back."),
            "Acme Corp",
        )
        assert "Voicemail: Hi, please call me back." in rendered


class TestRenderEmail:
    def test_with_body_text(self):
        rendered = _render_email(
            _email(subject="Proposal follow-up", body_text="Hi Alice, here is the proposal."),
            "Acme Corp",
        )
        assert "Email: Proposal follow-up" in rendered
        assert "Lead: Acme Corp" in rendered
        assert "Hi Alice, here is the proposal." in rendered

    def test_html_body_fallback(self):
        rendered = _render_email(
            _email(body_text=None, body_html="<p>Hello <b>world</b></p>"),
            "Acme Corp",
        )
        assert "Hello world" in rendered
        assert "<p>" not in rendered

    def test_no_body(self):
        rendered = _render_email(
            _email(body_text=None, body_html=None), "Acme Corp"
        )
        assert "Email:" in rendered
        assert "Lead: Acme Corp" in rendered


# ---------------------------------------------------------------------------
# Helpers (DB-free)
# ---------------------------------------------------------------------------


def test_strip_html():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert _strip_html("Plain text") == "Plain text"
    assert "Line 1" in _strip_html("<br/>Line 1<br>Line 2")


def test_truncate():
    assert _truncate("short", 100) == "short"
    assert "[truncated]" in _truncate("x" * 200, 100)


def test_content_hash_is_stable():
    h1 = _content_hash("hello")
    h2 = _content_hash("hello")
    assert h1 == h2
    assert _content_hash("different") != h1


def test_object_to_row_lead():
    row = _object_to_row(_lead(lead_id="lead_abc", name="Acme Corp"), "lead", "Acme Corp")
    assert row["id"] == "lead_abc"
    assert row["title"] == "Lead: Acme Corp"
    assert "Lead: Acme Corp" in row["content"]
    assert row["url"] == "https://app.close.com/lead/lead_abc/"
    assert row["_deleted"] is False


def test_object_to_row_call():
    row = _object_to_row(
        _call(call_id="acti_call_xyz", lead_id="lead_abc"),
        "call",
        "Acme Corp",
    )
    assert row["id"] == "acti_call_xyz"
    assert "Call" in row["title"]
    assert "Acme Corp" in row["title"]
    assert row["url"] == "https://app.close.com/lead/lead_abc/"


# ---------------------------------------------------------------------------
# Client: pagination and retry
# ---------------------------------------------------------------------------


def test_client_paginates(monkeypatch):
    fake = _FakeCloseAPI()
    fake.leads = {f"lead_{i}": _lead(lead_id=f"lead_{i}") for i in range(5)}
    client = _make_client(fake)

    monkeypatch.setattr("cognee_community_connector_close.close._PAGE_SIZE", 2)
    leads = list(client.iter_objects("/lead/"))
    assert len(leads) == 5


def test_client_filters_fields():
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Acme")}
    client = _make_client(fake)

    results = list(client.iter_objects("/lead/", fields=["id"]))
    assert results == [{"id": "lead_1"}]


def test_client_retries_429(monkeypatch):
    monkeypatch.setattr("cognee_community_connector_close.close.time.sleep", lambda _: None)
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead()}
    fake.next_error = 429
    fake.error_count = 2  # first 2 requests return 429, then OK
    client = _make_client(fake)

    result = client.request("GET", "/lead/")
    assert "data" in result


def test_client_retries_500(monkeypatch):
    monkeypatch.setattr("cognee_community_connector_close.close.time.sleep", lambda _: None)
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead()}
    fake.next_error = 500
    fake.error_count = 1
    client = _make_client(fake)

    result = client.request("GET", "/lead/")
    assert "data" in result


def test_retry_delay_reads_header():
    from cognee_community_connector_close.close import _retry_delay

    class FakeResp:
        headers = {"Retry-After": "3.5"}

    assert _retry_delay(FakeResp(), 0) == 3.5


def test_retry_delay_falls_back_to_backoff():
    from cognee_community_connector_close.close import _retry_delay

    class FakeResp:
        headers = {}

    assert _retry_delay(FakeResp(), 2) == 4.0  # 2^2


def test_get_me():
    fake = _FakeCloseAPI()
    client = _make_client(fake)
    me = client.get_me()
    assert me["organization_id"] == "org_test123"


# ---------------------------------------------------------------------------
# Type normalization
# ---------------------------------------------------------------------------


def test_normalize_types_default():
    assert _normalize_types(None) == DEFAULT_TYPES


def test_normalize_types_custom():
    assert _normalize_types(["call", "email"]) == ("call", "email")


def test_normalize_types_deduplicates():
    assert _normalize_types(["lead", "lead", "call"]) == ("lead", "call")


def test_normalize_types_invalid():
    with pytest.raises(ValueError, match="Unknown"):
        _normalize_types(["lead", "invalid_type"])


def test_normalize_types_empty():
    with pytest.raises(ValueError, match="empty"):
        _normalize_types([])


# ---------------------------------------------------------------------------
# Lead name lookup
# ---------------------------------------------------------------------------


def test_build_lead_lookup():
    fake = _FakeCloseAPI()
    fake.leads = {
        "lead_1": _lead(lead_id="lead_1", name="Alpha"),
        "lead_2": _lead(lead_id="lead_2", name="Beta"),
    }
    client = _make_client(fake)
    lookup = _build_lead_lookup(client)
    assert lookup == {"lead_1": "Alpha", "lead_2": "Beta"}


# ---------------------------------------------------------------------------
# State machine: sync + incremental + delete
# ---------------------------------------------------------------------------


def test_first_sync_yields_all_objects():
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Alpha")}
    fake.notes = {"note_1": _note(note_id="note_1", lead_id="lead_1")}
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    rows = list(_iter_rows(client, ("lead", "note"), state, stats))

    assert len(rows) == 2
    ids = {r["id"] for r in rows}
    assert ids == {"lead_1", "note_1"}
    assert stats["yielded"] == 2
    assert stats["deleted"] == 0
    assert "lead_1" in state["objects"]
    assert "note_1" in state["objects"]


def test_unchanged_objects_not_reyielded():
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Alpha")}
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    # First sync
    list(_iter_rows(client, ("lead",), state, stats))
    assert stats["yielded"] == 1

    # Second sync — same content, nothing yielded
    stats2: dict = {}
    rows = list(_iter_rows(client, ("lead",), state, stats2))
    assert len(rows) == 0
    assert stats2["unchanged"] == 1
    assert stats2["yielded"] == 0


def test_changed_content_is_reyielded():
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Alpha")}
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    list(_iter_rows(client, ("lead",), state, stats))

    # Change the lead
    fake.leads["lead_1"] = _lead(lead_id="lead_1", name="Alpha Renamed")
    stats2: dict = {}
    rows = list(_iter_rows(client, ("lead",), state, stats2))
    assert len(rows) == 1
    assert "Alpha Renamed" in rows[0]["content"]
    assert stats2["yielded"] == 1


def test_deleted_object_produces_tombstone():
    fake = _FakeCloseAPI()
    fake.leads = {
        "lead_1": _lead(lead_id="lead_1", name="Alpha"),
        "lead_2": _lead(lead_id="lead_2", name="Beta"),
    }
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    list(_iter_rows(client, ("lead",), state, stats))
    assert "lead_1" in state["objects"]
    assert "lead_2" in state["objects"]

    # Remove lead_1 from the API
    del fake.leads["lead_1"]
    stats2: dict = {}
    rows = list(_iter_rows(client, ("lead",), state, stats2))

    tombstones = [r for r in rows if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "lead_1"
    assert stats2["deleted"] == 1
    assert "lead_1" not in state["objects"]


def test_deletion_is_the_only_change():
    """A sync where the only event is a deletion must still produce a tombstone."""
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Alpha")}
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    list(_iter_rows(client, ("lead",), state, stats))

    # Remove it — nothing else changed
    del fake.leads["lead_1"]
    stats2: dict = {}
    rows = list(_iter_rows(client, ("lead",), state, stats2))

    assert rows == [{"id": "lead_1", "_deleted": True}]
    assert stats2["deleted"] == 1
    assert stats2["yielded"] == 0


def test_new_object_added_between_syncs():
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Alpha")}
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    list(_iter_rows(client, ("lead",), state, stats))

    # Add a new lead
    fake.leads["lead_2"] = _lead(lead_id="lead_2", name="Beta")
    stats2: dict = {}
    rows = list(_iter_rows(client, ("lead",), state, stats2))

    # Only the new lead is yielded; the unchanged one is skipped
    assert len(rows) == 1
    assert rows[0]["id"] == "lead_2"
    assert stats2["yielded"] == 1
    assert stats2["unchanged"] == 1


def test_activities_include_lead_context():
    """Notes, calls, emails should embed the parent lead's name."""
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Acme Corp")}
    fake.notes = {"note_1": _note(note_id="note_1", lead_id="lead_1")}
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    rows = list(_iter_rows(client, ("note",), state, stats))
    assert len(rows) == 1
    assert "Acme Corp" in rows[0]["content"]


def test_call_with_transcript_syncs_correctly():
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Acme Corp")}
    fake.calls = {
        "call_1": _call(
            call_id="call_1",
            lead_id="lead_1",
            summary="Discussed roadmap.",
            recording_transcript="Agent: Hello.\nCustomer: Hi there.",
        )
    }
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    rows = list(_iter_rows(client, ("call",), state, stats))
    assert len(rows) == 1
    assert "Discussed roadmap." in rows[0]["content"]
    assert "Agent: Hello." in rows[0]["content"]


def test_mixed_types_sync():
    """Leads and activities sync together with correct context."""
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Acme Corp")}
    fake.opportunities = {"oppo_1": _opportunity(opp_id="oppo_1", lead_id="lead_1")}
    fake.calls = {"call_1": _call(call_id="call_1", lead_id="lead_1")}
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    rows = list(_iter_rows(client, ("lead", "opportunity", "call"), state, stats))
    assert len(rows) == 3
    ids = {r["id"] for r in rows}
    assert ids == {"lead_1", "oppo_1", "call_1"}
    assert stats["scanned"] == 3


def test_edit_and_delete_in_same_sync():
    """One object edited, another deleted — both are handled correctly."""
    fake = _FakeCloseAPI()
    fake.leads = {
        "lead_1": _lead(lead_id="lead_1", name="Alpha"),
        "lead_2": _lead(lead_id="lead_2", name="Beta"),
    }
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    list(_iter_rows(client, ("lead",), state, stats))

    # Edit lead_1, delete lead_2
    fake.leads["lead_1"] = _lead(lead_id="lead_1", name="Alpha v2")
    del fake.leads["lead_2"]
    stats2: dict = {}
    rows = list(_iter_rows(client, ("lead",), state, stats2))

    tombstones = [r for r in rows if r.get("_deleted")]
    changes = [r for r in rows if not r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "lead_2"
    assert len(changes) == 1
    assert "Alpha v2" in changes[0]["content"]


def test_tombstones_come_before_rows():
    """Delete tombstones are yielded before new/changed rows."""
    fake = _FakeCloseAPI()
    fake.leads = {
        "lead_1": _lead(lead_id="lead_1", name="Alpha"),
        "lead_2": _lead(lead_id="lead_2", name="Beta"),
    }
    client = _make_client(fake)
    state: dict = {}
    stats: dict = {}

    list(_iter_rows(client, ("lead",), state, stats))

    del fake.leads["lead_1"]
    fake.leads["lead_3"] = _lead(lead_id="lead_3", name="Gamma")
    stats2: dict = {}
    rows = list(_iter_rows(client, ("lead",), state, stats2))

    # First row should be the tombstone, then the new object
    assert rows[0] == {"id": "lead_1", "_deleted": True}
    assert rows[-1]["id"] == "lead_3"


# ---------------------------------------------------------------------------
# close_source() factory
# ---------------------------------------------------------------------------


def test_close_source_requires_api_key():
    with pytest.raises(ValueError, match="API key"):
        close_source()


def test_close_source_reads_api_key_from_env(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.setenv("CLOSE_API_KEY", "api_test123")
    fake = _FakeCloseAPI()
    transport = httpx.MockTransport(fake.handler)
    http = httpx.Client(transport=transport)

    # Should not raise — reads CLOSE_API_KEY from env.
    resource = close_source(http_client=http)
    assert resource.name == "close_documents"


def test_close_source_resource_opts_into_document_mode():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    fake = _FakeCloseAPI()
    transport = httpx.MockTransport(fake.handler)
    http = httpx.Client(transport=transport)

    resource = close_source(api_key="test-key", http_client=http)

    assert resource.name == "close_documents"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR, None) == CLOSE_SOURCE_NAME
    assert getattr(resource, PIPELINE_SCOPE_ATTR, None) == "close_documents"
    assert resource.cognee_sync_stats == {}


def test_close_source_custom_resource_name_and_types():
    pytest.importorskip("dlt")
    fake = _FakeCloseAPI()
    transport = httpx.MockTransport(fake.handler)
    http = httpx.Client(transport=transport)

    resource = close_source(
        api_key="test-key",
        http_client=http,
        resource_name="close_acme",
        selected_types=["lead", "call"],
    )
    assert resource.name == "close_acme"


# ---------------------------------------------------------------------------
# dlt pipeline: incremental merge + hard-delete (needs dlt + sqlalchemy)
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, fake, selected_types=("lead",), resource_name="close_test"):
    """Run close_source through a dlt pipeline into a temp sqlite destination."""
    transport = httpx.MockTransport(fake.handler)
    http = httpx.Client(transport=transport)
    db_path = (tmp_path / "close.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="close_pipeline_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="close_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    resource = close_source(
        api_key="test-key",
        http_client=http,
        selected_types=list(selected_types),
        resource_name=resource_name,
    )
    pipeline.run(resource)
    return pipeline


def _read_rows(pipeline, table_name="close_test"):
    """Return {id: row-dict} for the connector's table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content FROM {table_name}") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_into_pipeline(dlt_mod, tmp_path):
    fake = _FakeCloseAPI()
    fake.leads = {
        "lead_1": _lead(lead_id="lead_1", name="Alpha"),
        "lead_2": _lead(lead_id="lead_2", name="Beta"),
    }
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    rows = _read_rows(pipeline)
    assert set(rows) == {"lead_1", "lead_2"}
    assert "Alpha" in rows["lead_1"]["content"]
    assert "Beta" in rows["lead_2"]["content"]


def test_edit_reflected_on_resync(dlt_mod, tmp_path):
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Alpha")}
    _run_sync(dlt_mod, tmp_path, fake)

    # Edit the lead
    fake.leads["lead_1"] = _lead(lead_id="lead_1", name="Alpha Renamed")
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    rows = _read_rows(pipeline)
    assert "Alpha Renamed" in rows["lead_1"]["content"]
    assert "Alpha\n" not in rows["lead_1"]["content"]  # old name is gone


def test_deleted_object_removed_from_pipeline(dlt_mod, tmp_path):
    fake = _FakeCloseAPI()
    fake.leads = {
        "lead_1": _lead(lead_id="lead_1", name="Alpha"),
        "lead_2": _lead(lead_id="lead_2", name="Beta"),
    }
    _run_sync(dlt_mod, tmp_path, fake)

    # Remove lead_1
    del fake.leads["lead_1"]
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    rows = _read_rows(pipeline)
    # lead_1 removed via hard-delete tombstone
    assert "lead_1" not in rows
    assert "lead_2" in rows


def test_deletion_only_sync_removes_from_pipeline(dlt_mod, tmp_path):
    """A sync where the only change is a deletion still removes the record."""
    fake = _FakeCloseAPI()
    fake.leads = {"lead_1": _lead(lead_id="lead_1", name="Alpha")}
    _run_sync(dlt_mod, tmp_path, fake)

    del fake.leads["lead_1"]
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    rows = _read_rows(pipeline)
    assert "lead_1" not in rows