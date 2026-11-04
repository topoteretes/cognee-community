"""Unit and integration test suite for Cognee Slite data-source connector."""

import pytest
from unittest.mock import MagicMock, patch
import requests

from cognee_community_connector_slite import (
    SliteAccessError,
    SliteAPIError,
    SliteAuthError,
    SliteClient,
    SliteNotFoundError,
    SlitePayloadTooLargeError,
    SliteRateLimitedError,
    SliteValidationError,
    _iter_notes,
    render_note,
    slite_source,
)
from fake_slite import FakeSlite


# ---------------------------------------------------------------------------
# 1. Client Unit Tests
# ---------------------------------------------------------------------------
class TestSliteClient:
    def test_client_init_validation(self):
        with pytest.raises(ValueError, match="empty or has an invalid format"):
            SliteClient("")
        with pytest.raises(ValueError, match="empty or has an invalid format"):
            SliteClient("bad key with spaces")

        client = SliteClient("secret_key_123")
        assert repr(client) == "SliteClient(base_url='https://api.slite.com/v1', api_key=<redacted>)"
        headers = client._headers()
        assert headers["Authorization"] == "Bearer secret_key_123"
        assert headers["x-slite-api-key"] == "secret_key_123"
        assert "SliteCogneeConnector" in headers["User-Agent"]

    @patch("requests.request")
    def test_client_request_success(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"user": {"id": "u1"}}
        mock_resp.headers = {"x-ratelimit-remaining": "85"}
        mock_req.return_value = mock_resp

        client = SliteClient("key_123")
        res = client.get_me()
        assert res == {"user": {"id": "u1"}}
        assert client.rate_limit_remaining == 85

    @patch("requests.request")
    def test_client_error_mappings(self, mock_req):
        client = SliteClient("test_key")

        # 401
        resp_401 = MagicMock(status_code=401)
        mock_req.return_value = resp_401
        with pytest.raises(SliteAuthError, match="auth/unauthorized"):
            client.get_me()

        # 403
        resp_403 = MagicMock(status_code=403)
        mock_req.return_value = resp_403
        with pytest.raises(SliteAccessError, match="Access forbidden"):
            client.get_notes()

        # 404
        resp_404 = MagicMock(status_code=404)
        mock_req.return_value = resp_404
        with pytest.raises(SliteNotFoundError, match="not found"):
            client.get_note("n_missing")

        # 413
        resp_413 = MagicMock(status_code=413)
        mock_req.return_value = resp_413
        with pytest.raises(SlitePayloadTooLargeError, match="too large"):
            client.get_note("n_huge")

        # 422
        resp_422 = MagicMock(status_code=422)
        mock_req.return_value = resp_422
        with pytest.raises(SliteValidationError, match="validation error"):
            client.search_notes()

        # 429
        resp_429 = MagicMock(status_code=429)
        mock_req.return_value = resp_429
        with pytest.raises(SliteRateLimitedError, match="rate limit exceeded"):
            client.get_notes()

    @patch("requests.request")
    def test_client_retries_on_500(self, mock_req):
        sleep_mock = MagicMock()
        client = SliteClient("test_key", sleep=sleep_mock)

        resp_500 = MagicMock(status_code=500)
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {"notes": []}
        resp_200.headers = {}
        mock_req.side_effect = [resp_500, resp_200]

        res = client.get_notes()
        assert res == {"notes": []}
        assert mock_req.call_count == 2
        sleep_mock.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# 2. Rendering and Document Formatting
# ---------------------------------------------------------------------------
class TestSliteFormatting:
    def test_render_note_document(self):
        note = {
            "id": "doc_99",
            "title": "Incident Management Runbook",
            "reviewState": "Verified",
            "channel": "DevOps",
            "url": "https://slite.com/app/docs/doc_99",
            "owner": {"name": "Bob Admin"},
            "createdAt": "2026-10-15T09:00:00Z",
            "lastEditedAt": "2026-11-01T14:30:00Z",
        }
        body = "# Incident Runbook\n\n1. Alert oncall\n2. Open war room"
        doc = render_note(note, body_markdown=body)

        assert doc["id"] == "slite:note:doc_99"
        assert doc["title"] == "Incident Management Runbook"
        assert doc["review_state"] == "Verified"
        assert doc["owner"] == "Bob Admin"
        assert doc["channel"] == "DevOps"
        assert "slite:channel:DevOps" in doc["node_sets"]
        assert "slite:review:Verified" in doc["node_sets"]
        assert "Alert oncall" in doc["text"]
        assert doc["_deleted"] is False


# ---------------------------------------------------------------------------
# 3. Ingestion, Change Detection & Incremental Sync
# ---------------------------------------------------------------------------
class TestSliteSyncAndChangeDetection:
    def test_first_sync_ingests_all_notes(self):
        fake = FakeSlite()
        fake.add_note("n1", "Refund Policy", content="Refunds within 14 days", review_state="Verified")
        fake.add_note("n2", "Deploy Checklist", content="Run migrations first", review_state="Outdated")

        state = {}
        stats = {}
        docs = list(_iter_notes(fake, state, stats))

        assert len(docs) == 2
        assert stats["scanned"] == 2
        assert stats["fetched"] == 2
        assert stats["skipped_unchanged"] == 0
        assert stats["deleted"] == 0

        doc_map = {d["id"]: d for d in docs}
        assert "slite:note:n1" in doc_map
        assert doc_map["slite:note:n1"]["review_state"] == "Verified"
        assert "Refunds within 14 days" in doc_map["slite:note:n1"]["content"]
        assert "slite:review:Verified" in doc_map["slite:note:n1"]["node_sets"]

        assert "slite:note:n2" in doc_map
        assert doc_map["slite:note:n2"]["review_state"] == "Outdated"
        assert "slite:review:Outdated" in doc_map["slite:note:n2"]["node_sets"]

    def test_second_sync_skips_unchanged_notes(self):
        fake = FakeSlite()
        fake.add_note("n1", "Security Policy", content="Rotate keys every 90 days")

        state = {}
        stats = {}
        # First sync
        first_docs = list(_iter_notes(fake, state, stats))
        assert len(first_docs) == 1
        assert stats["fetched"] == 1

        # Second sync without changes
        stats2 = {}
        second_docs = list(_iter_notes(fake, state, stats2))
        assert len(second_docs) == 0
        assert stats2["scanned"] == 1
        assert stats2["skipped_unchanged"] == 1
        assert stats2["fetched"] == 0

    def test_edited_note_re_fetches_body(self):
        fake = FakeSlite()
        fake.add_note("n1", "Architecture", content="V1 system")

        state = {}
        stats1 = {}
        list(_iter_notes(fake, state, stats1))

        # Update note in fake
        fake.notes["n1"]["content"] = "V2 system architecture"
        fake.notes["n1"]["lastEditedAt"] = "2026-11-04T12:00:00.000Z"

        stats2 = {}
        docs2 = list(_iter_notes(fake, state, stats2))
        assert len(docs2) == 1
        assert stats2["fetched"] == 1
        assert docs2[0]["content"] == "V2 system architecture"

    def test_handle_413_payload_too_large(self):
        fake = FakeSlite()
        fake.add_note("n_big", "Giant Database Dump", content="100MB text")
        fake.fail_paths["notes/n_big"] = SlitePayloadTooLargeError("Too large (413)")

        state = {}
        stats = {}
        docs = list(_iter_notes(fake, state, stats))

        assert len(docs) == 1
        assert stats["skipped_large"] == 1
        assert "too large to sync" in docs[0]["content"]

    def test_subtree_selection(self):
        fake = FakeSlite()
        fake.add_note("parent1", "Engineering Root")
        fake.add_note("child1", "Frontend Guidelines", parent_id="parent1")
        fake.add_note("child2", "Backend Guidelines", parent_id="parent1")
        fake.add_note("unrelated", "Marketing Copy")

        state = {}
        stats = {}
        docs = list(_iter_notes(fake, state, stats, parent_note_ids=["parent1"]))

        doc_ids = {d["id"] for d in docs}
        assert doc_ids == {"slite:note:parent1", "slite:note:child1", "slite:note:child2"}
        assert "slite:note:unrelated" not in doc_ids


# ---------------------------------------------------------------------------
# 4. Forget-on-Delete & Safety Check
# ---------------------------------------------------------------------------
class TestSliteForgetOnDelete:
    def test_archived_note_tombstoned(self):
        fake = FakeSlite()
        fake.add_note("n1", "Active Doc", content="Active")
        fake.add_note("n2", "Old Plan", content="Old", archived=False)

        state = {}
        stats = {}
        list(_iter_notes(fake, state, stats))

        # Archive n2
        fake.archive_note("n2")

        stats2 = {}
        docs2 = list(_iter_notes(fake, state, stats2, include_archived=False))
        assert len(docs2) == 1
        assert docs2[0]["id"] == "slite:note:n2"
        assert docs2[0]["_deleted"] is True
        assert stats2["deleted"] == 1

    def test_hard_deleted_note_tombstoned(self):
        fake = FakeSlite()
        fake.add_note("n1", "Doc 1")
        fake.add_note("n2", "Doc 2")

        state = {}
        stats = {}
        list(_iter_notes(fake, state, stats))

        # Permanently delete Doc 1
        fake.delete_note("n1")

        stats2 = {}
        docs2 = list(_iter_notes(fake, state, stats2))
        assert len(docs2) == 1
        assert docs2[0]["id"] == "slite:note:n1"
        assert docs2[0]["_deleted"] is True
        assert stats2["deleted"] == 1

    def test_safety_check_aborts_on_key_owner_change(self):
        fake = FakeSlite(user_id="u_alice", team_id="team_acme")
        fake.add_note("n1", "Secret Policy")

        state = {}
        stats = {}
        list(_iter_notes(fake, state, stats))

        # Simulating key owner change in Slite /me
        fake.user_id = "u_mallory"
        fake.team_id = "team_evil"

        with pytest.raises(SliteAuthError, match="API key owner changed"):
            list(_iter_notes(fake, state, stats))


# ---------------------------------------------------------------------------
# 5. Public Source Factory
# ---------------------------------------------------------------------------
class TestSliteSourceFactory:
    def test_source_factory_validation(self):
        with pytest.raises(ValueError, match="non-empty resource_name"):
            slite_source(resource_name="", api_key="secret")
        with pytest.raises(ValueError, match="requires an api_key or a service"):
            slite_source(resource_name="test")

    def test_source_factory_iteration(self):
        fake = FakeSlite()
        fake.add_note("doc1", "Knowledge Base")

        source = slite_source(resource_name="my_slite", service=fake)
        docs = list(source)
        assert len(docs) == 1
        assert docs[0]["id"] == "slite:note:doc1"
