import copy

import dlt
import pytest
import requests

from cognee_community_connector_jira_service_management import jira_service_management as jsm
from cognee_community_connector_jira_service_management import (
    jira_service_management_source,
    list_service_desks,
)

URL = "https://example.atlassian.net"


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class Jira:
    def __init__(self):
        self.ids = ["1", "2"]
        self.calls = []
        self.title = "Need access"
        self.updated = "2026-10-18T10:00:00.000+0000"
        self.error = None
        self.hidden = set()
        self.active = False
        self.elapsed = 1000
        self.breached = False
        self.request_type_fields = [
            {
                "id": "customfield_10010",
                "name": "Request Type",
                "schema": {"custom": "com.atlassian.servicedesk:vp-origin"},
            },
            {
                "id": "customfield_99999",
                "name": "Customer Request Type",
                "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:textfield"},
            },
        ]

    def get(self, url, params, **kwargs):
        self.calls.append((url, dict(params)))
        path = url.removeprefix(URL)
        if self.error and self.error[0] in path:
            return Response({}, self.error[1])
        if path == "/rest/api/3/field":
            return Response(self.request_type_fields)
        if path.endswith("/servicedesk/10"):
            return Response({"id": "10", "projectId": "100"})
        if path.endswith("/search/jql"):
            if "cf[10010] IS NOT EMPTY" not in params["jql"]:
                return Response({"issues": [], "isLast": True})
            values = [
                {"id": i, "fields": {"updated": self.updated}}
                for i in self.ids
                if i not in self.hidden
            ]
            start = int(params.get("nextPageToken", 0))
            result = {"issues": values[start : start + 1], "isLast": start + 1 >= len(values)}
            if not result["isLast"]:
                result["nextPageToken"] = str(start + 1)
            return Response(result)
        if path.endswith("/servicedesk"):
            return Response({"values": [{"id": "10", "projectId": "100"}], "isLastPage": True})
        parts = path.split("/")
        issue_id = parts[4]
        if issue_id not in self.ids:
            return Response({}, 404)
        if path.endswith("/comment"):
            values = [
                {"id": "a", "body": "customer question", "public": True},
                {"id": "b", "body": "secret agent note", "public": False},
                {"id": "c", "body": "unknown visibility"},
            ]
            start = params["start"]
            return Response({"values": values[start : start + 1], "isLastPage": start == 2})
        if path.endswith("/sla"):
            value = {"name": "Resolution", "completedCycles": []}
            if self.active:
                value["ongoingCycle"] = {
                    "breached": self.breached,
                    "elapsedTime": {"millis": self.elapsed},
                    "remainingTime": {"millis": 100000 - self.elapsed},
                }
            value["_links"] = {"self": f"https://example.atlassian.net/sla/{self.elapsed}"}
            return Response({"values": [value], "isLastPage": True})
        return Response(
            {
                "issueId": issue_id,
                "issueKey": f"HELP-{issue_id}",
                "serviceDeskId": "10",
                "summary": self.title,
                "currentStatus": {"status": "Open"},
                "requestFieldValues": [
                    {"fieldId": "summary", "value": self.title},
                    {
                        "fieldId": "description",
                        "value": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Please help"}],
                                }
                            ],
                        },
                    },
                ],
            }
        )


def sync(client, state, **kwargs):
    return list(jsm.sync_requests(client, URL, state, service_desk_ids=["10"], **kwargs))


def test_ingest_pagination_and_private_comments():
    client, state = Jira(), {}
    rows = sync(client, state)
    assert {r["id"] for r in rows} == {"1", "2"}
    assert rows[0]["title"] == "Need access"
    assert "Please help" in rows[0]["content"]
    assert "customer question" in rows[0]["content"]
    assert "Resolution" in rows[0]["content"]
    assert "secret" not in rows[0]["content"]
    assert "unknown visibility" not in rows[0]["content"]
    rows = sync(client, {}, include_internal_comments=True)
    assert "secret agent note" in rows[0]["content"]
    assert list_service_desks(base_url=URL, session=client)[0]["id"] == "10"


@pytest.mark.parametrize("name", ["Request Type", "Customer Request Type", "Anfragetyp"])
def test_request_type_is_resolved_by_schema_not_label(name):
    client = Jira()
    client.request_type_fields[0]["name"] = name
    assert len(sync(client, {})) == 2
    queries = [params["jql"] for url, params in client.calls if url.endswith("/search/jql")]
    assert queries
    assert all("cf[10010] IS NOT EMPTY" in query for query in queries)
    assert all("Customer Request Type" not in query for query in queries)


@pytest.mark.parametrize(
    "fields",
    [
        [],
        [
            {"id": "customfield_1", "schema": {"custom": "com.atlassian.servicedesk:vp-origin"}},
            {"id": "customfield_2", "schema": {"custom": "com.atlassian.servicedesk:vp-origin"}},
        ],
        [
            {
                "id": "customfield_invalid",
                "schema": {"custom": "com.atlassian.servicedesk:vp-origin"},
            }
        ],
    ],
)
def test_missing_ambiguous_or_invalid_field_preserves_state(fields):
    client, state = Jira(), {}
    sync(client, state)
    before = copy.deepcopy(state)
    client.request_type_fields = fields
    with pytest.raises(ValueError, match="field"):
        sync(client, state)
    assert state == before


def test_incremental_overlap_and_unchanged_hash(monkeypatch):
    client, state = Jira(), {}
    monkeypatch.setattr(jsm.time, "time", lambda: 1000)
    sync(client, state)
    assert state["cursor_ms"] == 1000000
    assert sync(client, state) == []
    assert any("updated >= 700000" in params.get("jql", "") for _, params in client.calls)
    client.title = "Updated request"
    assert len(sync(client, state)) == 2


def test_sla_clock_ticks_do_not_reingest_but_breach_does():
    client, state = Jira(), {}
    client.active = True
    rows = sync(client, state)
    assert "Resolution: ongoing, not breached" in rows[0]["content"]
    assert "millis" not in rows[0]["content"]
    assert "_links" not in rows[0]["content"]
    client.elapsed += 10000
    assert sync(client, state) == []
    client.breached = True
    rows = sync(client, state)
    assert len(rows) == 2
    assert "Resolution: ongoing, breached" in rows[0]["content"]


@pytest.mark.parametrize(
    ("sla", "expected"),
    [
        (
            {"name": "Resolution", "ongoingCycle": {"paused": True, "breached": False}},
            "Resolution: paused, not breached",
        ),
        (
            {
                "name": "Resolution",
                "completedCycles": [
                    {"stopTime": {"epochMillis": 200}, "breached": True},
                    {"stopTime": {"epochMillis": 100}, "breached": False},
                ],
            },
            "Resolution: completed, breached",
        ),
        ({"name": "Resolution"}, "Resolution: not started"),
    ],
)
def test_sla_states(sla, expected):
    assert jsm._sla_text(sla) == expected


def test_delete_including_last_request():
    client, state = Jira(), {}
    sync(client, state)
    client.ids = ["2"]
    assert sync(client, state) == [{"id": "1", "_deleted": True}]
    client.ids = []
    assert sync(client, state) == [{"id": "2", "_deleted": True}]
    assert state["documents"] == {}


def test_search_omission_does_not_delete_visible_request():
    client, state = Jira(), {}
    sync(client, state)
    client.hidden = {"1", "2"}
    assert sync(client, state) == []
    assert set(state["documents"]) == {"1", "2"}


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_failure_keeps_state(status):
    client, state = Jira(), {}
    sync(client, state)
    before = copy.deepcopy(state)
    client.error = ("/comment", status)
    with pytest.raises(requests.HTTPError):
        sync(client, state)
    assert state == before


def test_broken_pagination_aborts():
    class Broken:
        def get(self, *args, **kwargs):
            return Response({"values": [], "isLastPage": False, "issues": [], "isLast": False})

    with pytest.raises(ValueError, match="progress"):
        list(jsm._pages(Broken(), URL, "/path"))
    with pytest.raises(ValueError, match="pagination"):
        list(jsm._search(Broken(), URL, "project = 100", "id"))


def test_options_and_session_auth():
    client = jsm._make_session("user@example.com", "token")
    assert client.auth == ("user@example.com", "token")
    assert 429 in client.adapters["https://"].max_retries.status_forcelist
    client.close()
    rows = sync(Jira(), {}, include_comments=False, include_sla=False)
    assert "comment" not in rows[0]["content"]
    assert "SLA" not in rows[0]["content"]
    with pytest.raises(ValueError):
        jira_service_management_source(base_url=URL, service_desk_ids=[], session=Jira())


def test_active_sla_refresh_without_jql_change(monkeypatch):
    client, state = Jira(), {}
    client.active = True
    sync(client, state)
    original = jsm._search

    def no_delta(session, base_url, jql, fields):
        return iter([]) if fields == "updated" else original(session, base_url, jql, fields)

    monkeypatch.setattr(jsm, "_search", no_delta)
    client.active = False
    rows = sync(client, state)
    assert len(rows) == 2
    assert not any(doc["active_sla"] for doc in state["documents"].values())


def test_old_newly_visible_request_is_not_lost_to_cursor(monkeypatch):
    client, state = Jira(), {}
    sync(client, state)
    client.ids.append("3")
    original = jsm._search

    def no_delta(session, base_url, jql, fields):
        return iter([]) if fields == "updated" else original(session, base_url, jql, fields)

    monkeypatch.setattr(jsm, "_search", no_delta)
    rows = sync(client, state)
    assert [row["id"] for row in rows] == ["3"]


def test_missing_request_permission_failure_keeps_state():
    client, state = Jira(), {}
    sync(client, state)
    before = copy.deepcopy(state)
    client.hidden = {"1"}
    client.error = ("/request/1", 403)
    with pytest.raises(requests.HTTPError):
        sync(client, state)
    assert state == before


def test_partial_consumption_does_not_commit_state():
    state = {}
    rows = jsm.sync_requests(Jira(), URL, state, service_desk_ids=["10"])
    next(rows)
    rows.close()
    assert state == {}


@pytest.mark.parametrize(
    "url", ["http://example.com", "https://user:pass@example.com", "https://example.com/path"]
)
def test_invalid_site_url(url):
    with pytest.raises(ValueError):
        jira_service_management_source(base_url=url, service_desk_ids=["10"], session=Jira())


def test_real_dlt_merge_update_delete_and_state(tmp_path):
    client = Jira()
    pipeline = dlt.pipeline(
        pipeline_name="jsm_test",
        pipelines_dir=str(tmp_path / "state"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}"),
        dataset_name="jsm",
    )

    def run():
        source = jira_service_management_source(
            base_url=URL, service_desk_ids=["10"], session=client
        )
        assert source.cognee_document_source == jsm.SOURCE_NAME
        pipeline.run(source)
        with pipeline.sql_client() as sql:
            return sql.execute_sql(f"SELECT id, title FROM {jsm.TABLE_NAME} ORDER BY id")

    assert len(run()) == 2
    assert len(run()) == 2
    client.title = "Edited"
    assert all(row[1] == "Edited" for row in run())
    client.ids = ["2"]
    assert run() == [("2", "Edited")]
    client.ids = []
    assert run() == []
