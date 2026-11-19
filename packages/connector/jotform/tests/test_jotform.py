import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cognee_community_connector_jotform.jotform import (
    JotformConnector,
    JotformForm,
    JotformSubmission,
)


@pytest.fixture
def api_key():
    return "test_jotform_api_key_123456"


def test_fetch_forms(api_key):
    async def run_test():
        forms_data = {
            "responseCode": 200,
            "content": [
                {
                    "id": "241001",
                    "title": "Property Maintenance Service Request",
                    "status": "ENABLED",
                    "created_at": "2026-10-01 10:00:00",
                    "updated_at": "2026-10-20 14:30:00",
                    "count": "24",
                }
            ],
        }

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=forms_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = JotformConnector(api_key=api_key, session=mock_session)
        forms = await connector.fetch_forms()
        assert len(forms) == 1
        f = forms[0]
        assert f.id == "241001"
        assert f.title == "Property Maintenance Service Request"
        assert f.submission_count == 24

        md = f.to_markdown()
        assert "# Form Overview: Property Maintenance Service Request" in md
        assert "**Form ID:** 241001" in md
        assert "**Total Submissions:** 24" in md

    asyncio.run(run_test())


def test_fetch_questions_and_exclude_payments(api_key):
    async def run_test():
        questions_data = {
            "responseCode": 200,
            "content": {
                "1": {"type": "control_head", "text": "Header"},
                "2": {"type": "control_textbox", "text": "Property Address"},
                "3": {"type": "control_dropdown", "text": "Service Type"},
                "4": {"type": "control_textarea", "text": "Issue Description"},
                "5": {"type": "control_payment", "text": "Payment Authorization"},
            },
        }

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=questions_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = JotformConnector(api_key=api_key, exclude_payment_fields=True, session=mock_session)
        q_map = await connector.fetch_questions("241001")
        assert q_map["2"] == "Property Address"
        assert q_map["3"] == "Service Type"
        assert q_map["4"] == "Issue Description"
        # Payment field must be excluded
        assert "5" not in q_map

    asyncio.run(run_test())


def test_fetch_submissions(api_key):
    async def run_test():
        submissions_data = {
            "responseCode": 200,
            "content": [
                {
                    "id": "sub_8801",
                    "created_at": "2026-10-22 09:15:00",
                    "status": "ACTIVE",
                    "answers": {
                        "2": {"answer": "123 Main St, Suite 400"},
                        "3": {"answer": "HVAC Cooling"},
                        "4": {"answer": "Upstairs AC unit is cycling on and off repeatedly."},
                    },
                }
            ],
        }

        mock_session = MagicMock()

        def mock_get(url, headers=None, params=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "questions" in url:
                mock_resp.json = AsyncMock(
                    return_value={
                        "responseCode": 200,
                        "content": {
                            "2": {"type": "control_textbox", "text": "Property Address"},
                            "3": {"type": "control_dropdown", "text": "Service Type"},
                            "4": {"type": "control_textarea", "text": "Issue Description"},
                        },
                    }
                )
            else:
                mock_resp.json = AsyncMock(return_value=submissions_data)

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = JotformConnector(api_key=api_key, session=mock_session)
        subs = await connector.fetch_submissions("241001", form_title="Property Maintenance")
        assert len(subs) == 1
        s = subs[0]
        assert s.id == "sub_8801"
        assert s.answers["Property Address"] == "123 Main St, Suite 400"
        assert s.answers["Service Type"] == "HVAC Cooling"

        md = s.to_markdown()
        assert "# Form Submission: Property Maintenance" in md
        assert "**Property Address:** 123 Main St, Suite 400" in md
        assert "**Service Type:** HVAC Cooling" in md
        assert "**Issue Description:** Upstairs AC unit is cycling on and off repeatedly." in md

    asyncio.run(run_test())


def test_auth_and_rate_limits(api_key):
    async def run_test():
        # 401 Unauthorized
        mock_session_401 = MagicMock()
        mock_resp_401 = MagicMock()
        mock_resp_401.status = 401
        cm_401 = MagicMock()
        cm_401.__aenter__ = AsyncMock(return_value=mock_resp_401)
        cm_401.__aexit__ = AsyncMock(return_value=None)
        mock_session_401.get.return_value = cm_401
        mock_session_401.closed = False

        connector_401 = JotformConnector(api_key=api_key, session=mock_session_401)
        with pytest.raises(PermissionError, match="Jotform authentication failed"):
            await connector_401.fetch_forms()

        # 429 Rate limit
        mock_session_429 = MagicMock()
        mock_resp_429 = MagicMock()
        mock_resp_429.status = 429
        cm_429 = MagicMock()
        cm_429.__aenter__ = AsyncMock(return_value=mock_resp_429)
        cm_429.__aexit__ = AsyncMock(return_value=None)
        mock_session_429.get.return_value = cm_429
        mock_session_429.closed = False

        connector_429 = JotformConnector(api_key=api_key, session=mock_session_429)
        with pytest.raises(RuntimeError, match="Jotform daily API rate limit exceeded"):
            await connector_429.fetch_forms()

    asyncio.run(run_test())


def test_to_documents(api_key):
    async def run_test():
        mock_session = MagicMock()

        def mock_get(url, headers=None, params=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "questions" in url:
                mock_resp.json = AsyncMock(
                    return_value={"responseCode": 200, "content": {"1": {"type": "control_textbox", "text": "Feedback"}}}
                )
            elif "submissions" in url:
                mock_resp.json = AsyncMock(
                    return_value={
                        "responseCode": 200,
                        "content": [{"id": "sub_1", "answers": {"1": {"answer": "Great service!"}}}],
                    }
                )
            else:
                mock_resp.json = AsyncMock(
                    return_value={"responseCode": 200, "content": [{"id": "form_1", "title": "Customer Feedback"}]}
                )

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = JotformConnector(api_key=api_key, session=mock_session)
        docs = await connector.to_documents()
        assert len(docs) == 2
        assert any("# Form Overview: Customer Feedback" in d for d in docs)
        assert any("# Form Submission: Customer Feedback" in d for d in docs)
        assert any("**Feedback:** Great service!" in d for d in docs)

    asyncio.run(run_test())
