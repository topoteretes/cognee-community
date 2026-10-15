import json

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from dlt.extract.exceptions import ResourceExtractionError
from dlt.extract.resource import DltResource

from cognee_community_connector_airtable import airtable as connector
from cognee_community_connector_airtable import airtable_source
from tests.fakes import FakeAirtableSession, response


def test_factory_marks_the_exact_returned_resource_and_resolves_explicit_pat(monkeypatch):
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "unused-environment-token")
    session = FakeAirtableSession()
    resource = airtable_source(base_id="appOne", token="explicit-token", session=session)
    assert isinstance(resource, DltResource)
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "airtable"
    assert getattr(resource, PIPELINE_SCOPE_ATTR)
    rows = list(resource)
    assert len(rows) == 2
    assert all(
        call["headers"]["Authorization"] == "Bearer explicit-token" for call in session.calls
    )
    assert "explicit-token" not in json.dumps(rows)
    assert not session.closed


def test_factory_resolves_environment_pat(monkeypatch):
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "environment-token")
    session = FakeAirtableSession()
    resource = airtable_source(base_id="appOne", session=session)
    list(resource)
    assert all(
        call["headers"]["Authorization"] == "Bearer environment-token" for call in session.calls
    )


def test_missing_pat_fails_before_http(monkeypatch):
    monkeypatch.delenv("AIRTABLE_ACCESS_TOKEN", raising=False)
    session = FakeAirtableSession()
    with pytest.raises(ValueError):
        airtable_source(base_id="appOne", session=session)
    assert session.calls == []


@pytest.mark.parametrize("base_id", ["", "app/escape", "app one", None])
def test_invalid_base_id_fails_before_http(base_id):
    session = FakeAirtableSession()
    with pytest.raises(ValueError):
        airtable_source(base_id=base_id, token="test-token", session=session)
    assert session.calls == []


@pytest.mark.parametrize("table_ids", [[""], ["tbl/escape"], [None]])
def test_invalid_table_id_fails_before_http(table_ids):
    session = FakeAirtableSession()
    with pytest.raises(ValueError):
        airtable_source(base_id="appOne", table_ids=table_ids, token="test-token", session=session)
    assert session.calls == []


@pytest.mark.parametrize("other_base", ["appTwo", "appone"])
def test_different_bases_get_different_pipeline_scopes_and_resource_names(other_base):
    first = airtable_source(base_id="appOne", token="test-token", session=FakeAirtableSession())
    second = airtable_source(base_id=other_base, token="test-token", session=FakeAirtableSession())
    assert first.name != second.name
    assert getattr(first, PIPELINE_SCOPE_ATTR) != getattr(second, PIPELINE_SCOPE_ATTR)


def test_injected_transport_headers_and_lifecycle_remain_owned_by_caller():
    session = FakeAirtableSession()
    session.headers = {"X-Acceptance-Header": "retained", "Authorization": "Bearer prior-token"}
    source = airtable_source(base_id="appOne", token="new-token", session=session)
    list(source)
    assert session.headers == {
        "X-Acceptance-Header": "retained",
        "Authorization": "Bearer prior-token",
    }
    assert all(call["headers"]["Authorization"] == "Bearer new-token" for call in session.calls)
    assert all(call["headers"]["X-Acceptance-Header"] == "retained" for call in session.calls)
    assert not session.closed


@pytest.mark.parametrize("failure", [False, True])
def test_connector_closes_its_own_session_after_success_or_failure(monkeypatch, failure):
    session = FakeAirtableSession()
    if failure:
        session.failures["/v0/meta/bases/appOne/tables"] = [response({}, 403)]
    monkeypatch.setattr(connector.requests, "Session", lambda: session)
    source = airtable_source(base_id="appOne", token="managed-private-token")
    if failure:
        with pytest.raises(ResourceExtractionError) as error:
            list(source)
        assert isinstance(error.value.__cause__, connector.AirtableError)
    else:
        assert len(list(source)) == 2
    assert session.closed
    assert all(
        call["headers"]["Authorization"] == "Bearer managed-private-token" for call in session.calls
    )


def test_empty_selection_explicitly_deselects_all_tables():
    session = FakeAirtableSession()
    resource = airtable_source(base_id="appOne", table_ids=[], token="test-token", session=session)
    assert list(resource) == []
    assert len(session.calls) == 1
    assert "/meta/" in session.calls[0]["path"]
