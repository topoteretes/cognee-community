"""Unit tests for the Deel connector."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_deel.deel import (
    DEEL_CONTRACTS_TABLE,
    DEEL_SOURCE_NAME,
    DEEL_WORKERS_TABLE,
    deel_source,
)


class FakeDeelClient:
    """Mock client imitating Deel REST API endpoints."""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = responses or {}
        self.call_history: list[tuple[str, dict]] = []

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        params = params or {}
        self.call_history.append((endpoint, params))

        if endpoint in self.responses:
            val = self.responses[endpoint]
            if callable(val):
                return val(params)
            return val

        # Handle parameterized endpoints like contracts/{id}
        for pattern, handler in self.responses.items():
            if pattern in endpoint:
                if callable(handler):
                    return handler(params)
                return handler

        return {"data": []}


def test_auth_token_required(monkeypatch):
    """Fails with ValueError if no token is passed and DEEL_API_TOKEN is unset."""
    monkeypatch.delenv("DEEL_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Deel API token required"):
        deel_source(token=None, client=None)


def test_source_metadata_document_mode():
    """Declares DOCUMENT_SOURCE_ATTR so Cognee processes records in document mode."""
    client = FakeDeelClient()
    source = deel_source(client=client)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == DEEL_SOURCE_NAME


def test_workers_ingestion_and_fields():
    """Worker directory entries are formatted into proper document rows."""
    people_data = {
        "data": [
            {
                "id": "person_101",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "job_title": "Lead Systems Architect",
                "department": "Engineering",
                "work_email": "ada@example.com",
                "country": "United Kingdom",
                "hiring_type": "Direct",
                "hiring_status": "Active",
                "start_date": "2024-01-15",
            }
        ],
        "page": {"total_rows": 1},
    }
    client = FakeDeelClient({"people": people_data})
    source = deel_source(client=client, include_contracts=False, include_workers=True)

    workers_res = next(res for res in source.resources.values() if res.name == DEEL_WORKERS_TABLE)
    assert workers_res.write_disposition == "replace"

    rows = list(workers_res)
    assert len(rows) == 1
    doc = rows[0]

    assert doc["id"] == "deel:worker:person_101"
    assert "Ada Lovelace" in doc["title"]
    assert "Lead Systems Architect" in doc["title"]
    assert "- **Full Name**: Ada Lovelace" in doc["content"]
    assert "- **Job Title**: Lead Systems Architect" in doc["content"]
    assert "- **Work Email**: ada@example.com" in doc["content"]
    assert "- **Location / Country**: United Kingdom" in doc["content"]


def test_contracts_ingestion_metadata_first():
    """Contracts default to metadata-only ingestion; sensitive body text is omitted."""
    contracts_data = {
        "data": [
            {
                "id": "cnt_999",
                "title": "Senior AI Engineer Agreement",
                "type": "fixed",
                "status": "in_progress",
                "currency": "USD",
                "created_at": "2024-02-01T10:00:00Z",
                "updated_at": "2024-03-01T12:00:00Z",
                "worker": {
                    "full_name": "Alan Turing",
                    "email": "alan@example.com",
                },
                "client": {"name": "Acme Corp"},
            }
        ],
        "page": {"cursor": None, "total_rows": 1},
    }
    client = FakeDeelClient(
        {
            "contracts": contracts_data,
            "contracts/cnt_999": {
                "data": {"scope_of_work": "Secret intellectual property clauses"}
            },
        }
    )
    source = deel_source(
        client=client,
        include_workers=False,
        include_contracts=True,
        include_contract_documents=False,  # default
    )

    contracts_res = next(
        res for res in source.resources.values() if res.name == DEEL_CONTRACTS_TABLE
    )
    assert contracts_res.write_disposition == "replace"

    rows = list(contracts_res)
    assert len(rows) == 1
    doc = rows[0]

    assert doc["id"] == "deel:contract:cnt_999"
    assert "Senior AI Engineer Agreement" in doc["title"]
    assert "- **Worker**: Alan Turing" in doc["content"]
    assert "- **Client**: Acme Corp" in doc["content"]
    assert "- **Type**: fixed" in doc["content"]
    # Sensitive body clauses must NOT be present
    assert "Secret intellectual property clauses" not in doc["content"]
    assert "Contract Clauses & Content" not in doc["content"]


def test_contracts_ingestion_with_opt_in_documents():
    """When include_contract_documents=True, full contract text is included."""
    contracts_data = {
        "data": [
            {
                "id": "cnt_777",
                "title": "Consulting Agreement",
                "type": "consultant",
                "status": "active",
                "worker": {"full_name": "Grace Hopper"},
                "client": {"name": "Tech Research"},
            }
        ]
    }
    detail_data = {
        "data": {
            "scope_of_work": "Design and build compiler architectures for next-generation systems."
        }
    }
    client = FakeDeelClient(
        {
            "contracts": contracts_data,
            "contracts/cnt_777": detail_data,
        }
    )
    source = deel_source(
        client=client,
        include_workers=False,
        include_contracts=True,
        include_contract_documents=True,
    )

    contracts_res = next(
        res for res in source.resources.values() if res.name == DEEL_CONTRACTS_TABLE
    )
    rows = list(contracts_res)
    assert len(rows) == 1
    doc = rows[0]

    assert "## Contract Clauses & Content" in doc["content"]
    assert "Design and build compiler architectures" in doc["content"]


def test_cursor_based_pagination():
    """Iterates across cursor-based pagination using after_cursor."""

    def contracts_handler(params):
        cursor = params.get("after_cursor")
        if not cursor:
            return {
                "data": [{"id": "c1", "title": "Contract 1"}],
                "page": {"cursor": "cursor_page_2", "total_rows": 2},
            }
        elif cursor == "cursor_page_2":
            return {
                "data": [{"id": "c2", "title": "Contract 2"}],
                "page": {"cursor": None, "total_rows": 2},
            }
        return {"data": []}

    client = FakeDeelClient({"contracts": contracts_handler})
    source = deel_source(client=client, include_workers=False, include_contracts=True)

    contracts_res = next(
        res for res in source.resources.values() if res.name == DEEL_CONTRACTS_TABLE
    )
    rows = list(contracts_res)
    assert len(rows) == 2
    assert rows[0]["id"] == "deel:contract:c1"
    assert rows[1]["id"] == "deel:contract:c2"


def test_incremental_sync_since_watermark():
    """Ignores contracts with updated_at earlier than since parameter."""
    contracts_data = {
        "data": [
            {
                "id": "c_old",
                "title": "Old Contract",
                "updated_at": "2023-01-01T00:00:00Z",
            },
            {
                "id": "c_new",
                "title": "New Contract",
                "updated_at": "2024-05-01T00:00:00Z",
            },
        ]
    }
    client = FakeDeelClient({"contracts": contracts_data})
    source = deel_source(
        client=client,
        include_workers=False,
        include_contracts=True,
        since="2024-01-01T00:00:00Z",
    )

    contracts_res = next(
        res for res in source.resources.values() if res.name == DEEL_CONTRACTS_TABLE
    )
    rows = list(contracts_res)
    assert len(rows) == 1
    assert rows[0]["id"] == "deel:contract:c_new"


def test_resource_selection_flags():
    """Respects include_workers and include_contracts flags."""
    client = FakeDeelClient()

    workers_only = deel_source(client=client, include_workers=True, include_contracts=False)
    assert DEEL_WORKERS_TABLE in workers_only.resources
    assert DEEL_CONTRACTS_TABLE not in workers_only.resources

    contracts_only = deel_source(client=client, include_workers=False, include_contracts=True)
    assert DEEL_CONTRACTS_TABLE in contracts_only.resources
    assert DEEL_WORKERS_TABLE not in contracts_only.resources


def test_contract_filters_types_and_statuses():
    """Passes contract types and statuses to API query params."""
    client = FakeDeelClient({"contracts": {"data": []}})
    source = deel_source(
        client=client,
        include_workers=False,
        include_contracts=True,
        contract_types=["eor", "fixed"],
        contract_statuses=["in_progress"],
    )
    contracts_res = next(
        res for res in source.resources.values() if res.name == DEEL_CONTRACTS_TABLE
    )
    list(contracts_res)

    endpoint, params = client.call_history[0]
    assert endpoint == "contracts"
    assert params["types[]"] == ["eor", "fixed"]
    assert params["statuses[]"] == ["in_progress"]


def test_offset_pagination_people():
    """Paginates across multiple offset pages for people directory."""

    def people_handler(params):
        offset = params.get("offset", 0)
        if offset == 0:
            return {
                "data": [{"id": f"p_{i}", "first_name": f"Worker {i}"} for i in range(50)],
                "page": {"total_rows": 60},
            }
        elif offset == 50:
            return {
                "data": [{"id": f"p_{i}", "first_name": f"Worker {i}"} for i in range(50, 60)],
                "page": {"total_rows": 60},
            }
        return {"data": []}

    client = FakeDeelClient({"people": people_handler})
    source = deel_source(client=client, include_workers=True, include_contracts=False)
    workers_res = next(res for res in source.resources.values() if res.name == DEEL_WORKERS_TABLE)
    rows = list(workers_res)
    assert len(rows) == 60
    assert rows[0]["id"] == "deel:worker:p_0"
    assert rows[59]["id"] == "deel:worker:p_59"


def test_retry_after_and_transient_helpers():
    """Validates _retry_after header parsing and exponential fallback."""
    import httpx

    from cognee_community_connector_deel.deel import _is_transient, _retry_after

    # Numeric header
    assert _retry_after({"retry-after": "5"}, 0) == 5.0
    assert _retry_after({"Retry-After": "10"}, 0) == 10.0
    # No header fallback to 2^attempt
    assert _retry_after({}, 2) == 4.0

    # Transient check
    assert _is_transient(httpx.ReadTimeout("timed out")) is True
    assert _is_transient(ValueError("invalid value")) is False


def test_filter_by_worker_ids():
    """Restricts ingested workers to specified worker_ids."""
    people_data = {
        "data": [
            {"id": "w1", "first_name": "Worker One"},
            {"id": "w2", "first_name": "Worker Two"},
            {"id": "w3", "first_name": "Worker Three"},
        ]
    }
    client = FakeDeelClient({"people": people_data})
    source = deel_source(
        client=client,
        include_workers=True,
        include_contracts=False,
        worker_ids=["w1", "w3"],
    )
    workers_res = next(res for res in source.resources.values() if res.name == DEEL_WORKERS_TABLE)
    rows = list(workers_res)
    assert len(rows) == 2
    assert [r["id"] for r in rows] == ["deel:worker:w1", "deel:worker:w3"]


def test_filter_by_contract_ids():
    """Restricts ingested contracts to specified contract_ids."""
    contracts_data = {
        "data": [
            {"id": "cnt_1", "title": "Contract One"},
            {"id": "cnt_2", "title": "Contract Two"},
        ]
    }
    client = FakeDeelClient({"contracts": contracts_data})
    source = deel_source(
        client=client,
        include_workers=False,
        include_contracts=True,
        contract_ids=["cnt_2"],
    )
    contracts_res = next(
        res for res in source.resources.values() if res.name == DEEL_CONTRACTS_TABLE
    )
    rows = list(contracts_res)
    assert len(rows) == 1
    assert rows[0]["id"] == "deel:contract:cnt_2"


def test_resource_state_cursor_advancement(monkeypatch):
    """Simulates dlt resource state and verifies cursor watermark advancement."""
    mock_state = {}

    import dlt

    monkeypatch.setattr(dlt.current, "resource_state", lambda: mock_state)

    contracts_data = {
        "data": [
            {"id": "c1", "title": "Contract 1", "updated_at": "2024-01-10T00:00:00Z"},
            {"id": "c2", "title": "Contract 2", "updated_at": "2024-03-15T00:00:00Z"},
        ]
    }
    client = FakeDeelClient({"contracts": contracts_data})
    source = deel_source(client=client, include_workers=False, include_contracts=True)
    contracts_res = next(
        res for res in source.resources.values() if res.name == DEEL_CONTRACTS_TABLE
    )
    list(contracts_res)

    assert mock_state.get("last_updated_at") == "2024-03-15T00:00:00Z"
