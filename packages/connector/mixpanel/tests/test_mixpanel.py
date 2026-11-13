from datetime import date

from cognee_community_connector_mixpanel.mixpanel import _artifact_row, _event_rows, _items


def test_artifact_row_is_stable_and_descriptive():
    row = _artifact_row("cohort", {"id": 7, "name": "Activated users", "definition": {"days": 14}})
    assert row["id"] == "cohort:7"
    assert row["title"] == "Mixpanel cohort: Activated users"
    assert '"days": 14' in row["content"]


def test_event_rows_prefer_insert_id_for_idempotency():
    rows = list(_event_rows([{"event": "Signed Up", "properties": {"$insert_id": "abc"}}]))
    assert rows[0]["id"] == "event:abc"
    assert rows[0]["title"] == "Mixpanel event: Signed Up"


def test_items_accepts_supported_envelopes():
    assert _items([{"id": 1}]) == [{"id": 1}]
    assert _items({"results": [{"id": 2}]}) == [{"id": 2}]
    assert _items({"data": [{"id": 3}]}) == [{"id": 3}]
    assert _items({"unknown": []}) == []


def test_invalid_event_window_is_rejected_before_pipeline_work(monkeypatch):
    import cognee_community_connector_mixpanel.mixpanel as module

    class FakeDlt:
        @staticmethod
        def resource(**kwargs):
            return lambda fn: fn

        @staticmethod
        def source(**kwargs):
            return lambda fn: fn

    monkeypatch.setitem(__import__("sys").modules, "dlt", FakeDlt)
    try:
        module.mixpanel_source(client=FakeClient(), include_events=True, event_start=date(2025, 2, 2), event_end=date(2025, 2, 1))
    except ValueError as exc:
        assert "event_start" in str(exc)
    else:
        raise AssertionError("Expected an invalid window to fail")


class FakeClient:
    def event_schemas(self): return []
    def cohorts(self): return []
    def saved_reports(self): return []
    def events(self, start, end): return []
