import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cognee_community_connector_lemlist.lemlist import (
    LemlistConnector,
    LemlistCampaign,
    LemlistMetrics,
)


@pytest.fixture
def api_key():
    return "test_lemlist_api_key_123456"


def test_fetch_campaigns_with_embedded_stats(api_key):
    async def run_test():
        campaigns_data = [
            {
                "_id": "cam_101",
                "name": "Q4 Enterprise DevRel Outreach",
                "status": "active",
                "createdAt": "2026-10-10T12:00:00.000Z",
                "updatedAt": "2026-10-22T16:45:00.000Z",
                "timezone": "America/New_York",
                "labels": ["enterprise", "devrel"],
                "stats": {
                    "emailsSent": 1250,
                    "emailsOpened": 625,
                    "emailsClicked": 150,
                    "emailsReplied": 125,
                    "emailsBounced": 10,
                    "unsubscribed": 5,
                },
            }
        ]

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=campaigns_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = LemlistConnector(api_key=api_key, session=mock_session)
        campaigns = await connector.fetch_campaigns()
        assert len(campaigns) == 1
        c = campaigns[0]
        assert c.id == "cam_101"
        assert c.name == "Q4 Enterprise DevRel Outreach"
        assert c.status == "active"
        assert c.labels == ["enterprise", "devrel"]
        assert c.metrics is not None
        assert c.metrics.emails_sent == 1250
        assert c.metrics.open_rate == 50.0
        assert c.metrics.reply_rate == 10.0

        md = c.to_markdown()
        assert "# Outreach Campaign: Q4 Enterprise DevRel Outreach" in md
        assert "**Status:** ACTIVE" in md
        assert "**Total Emails Dispatched:** 1,250" in md
        assert "**Unique Opens:** 625 (50.0%)" in md
        assert "**Direct Replies:** 125 (10.0%)" in md

    asyncio.run(run_test())


def test_fetch_campaign_stats_separate_call(api_key):
    async def run_test():
        campaigns_data = [
            {
                "_id": "cam_202",
                "name": "Founder Seed Sequence",
                "status": "paused",
                "createdAt": "2026-09-01T08:00:00.000Z",
                "updatedAt": "2026-10-01T09:00:00.000Z",
            }
        ]

        stats_data = {
            "emailsSent": 500,
            "emailsOpened": 300,
            "emailsClicked": 100,
            "emailsReplied": 80,
            "emailsBounced": 4,
            "unsubscribed": 2,
        }

        mock_session = MagicMock()

        def mock_get(url, headers=None, params=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "/stats" in url:
                mock_resp.json = AsyncMock(return_value=stats_data)
            else:
                mock_resp.json = AsyncMock(return_value=campaigns_data)

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = LemlistConnector(api_key=api_key, session=mock_session)
        campaigns = await connector.fetch_campaigns(include_stats=True)
        assert len(campaigns) == 1
        c = campaigns[0]
        assert c.metrics is not None
        assert c.metrics.emails_sent == 500
        assert c.metrics.open_rate == 60.0
        assert c.metrics.reply_rate == 16.0

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

        connector_401 = LemlistConnector(api_key=api_key, session=mock_session_401)
        with pytest.raises(PermissionError, match="lemlist authentication failed"):
            await connector_401.fetch_campaigns()

        # 429 Rate limit
        mock_session_429 = MagicMock()
        mock_resp_429 = MagicMock()
        mock_resp_429.status = 429
        cm_429 = MagicMock()
        cm_429.__aenter__ = AsyncMock(return_value=mock_resp_429)
        cm_429.__aexit__ = AsyncMock(return_value=None)
        mock_session_429.get.return_value = cm_429
        mock_session_429.closed = False

        connector_429 = LemlistConnector(api_key=api_key, session=mock_session_429)
        with pytest.raises(RuntimeError, match="lemlist API rate limit exceeded"):
            await connector_429.fetch_campaigns()

    asyncio.run(run_test())


def test_to_documents(api_key):
    async def run_test():
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value=[
                {
                    "_id": "cam_303",
                    "name": "Community Mergetober Drive",
                    "status": "active",
                    "stats": {"emailsSent": 2000, "emailsOpened": 1400, "emailsReplied": 350},
                }
            ]
        )
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = LemlistConnector(api_key=api_key, session=mock_session)
        docs = await connector.to_documents()
        assert len(docs) == 1
        assert "# Outreach Campaign: Community Mergetober Drive" in docs[0]
        assert "**Unique Opens:** 1,400 (70.0%)" in docs[0]

    asyncio.run(run_test())
