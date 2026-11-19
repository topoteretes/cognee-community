import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cognee_community_connector_bitrix24.bitrix24 import (
    Bitrix24Connector,
    Bitrix24Deal,
    Bitrix24Contact,
    Bitrix24Company,
    Bitrix24Comment,
)


@pytest.fixture
def webhook_url():
    return "https://sample.bitrix24.com/rest/1/secrettoken"


def test_fetch_deals_with_comments(webhook_url):
    async def run_test():
        deals_data = {
            "result": [
                {
                    "ID": "101",
                    "TITLE": "Enterprise Cloud Migration",
                    "STAGE_ID": "NEGOTIATION",
                    "OPPORTUNITY": "45000.00",
                    "CURRENCY_ID": "USD",
                    "COMPANY_ID": "42",
                    "CONTACT_ID": "7",
                    "DATE_CREATE": "2026-10-15T10:00:00+03:00",
                    "DATE_MODIFY": "2026-10-20T14:30:00+03:00",
                    "BEGINDATE": "2026-10-15",
                    "CLOSEDATE": "2026-11-30",
                }
            ]
        }

        comments_data = {
            "result": [
                {
                    "ID": "1",
                    "ENTITY_ID": "101",
                    "ENTITY_TYPE": "deal",
                    "AUTHOR_ID": "99",
                    "COMMENT": "<p>Client requested revised security architecture review.</p>",
                    "CREATED": "2026-10-18T11:20:00+03:00",
                }
            ]
        }

        mock_session = MagicMock()

        def mock_post(url, json=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "crm.deal.list" in url:
                mock_resp.json = AsyncMock(return_value=deals_data)
            elif "crm.timeline.comment.list" in url:
                mock_resp.json = AsyncMock(return_value=comments_data)
            else:
                mock_resp.json = AsyncMock(return_value={"result": []})
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.post.side_effect = mock_post
        mock_session.closed = False

        connector = Bitrix24Connector(webhook_url=webhook_url, session=mock_session)

        deals = await connector.fetch_deals(include_comments=True)
        assert len(deals) == 1
        deal = deals[0]
        assert deal.id == "101"
        assert deal.title == "Enterprise Cloud Migration"
        assert deal.opportunity == 45000.0
        assert deal.stage_id == "NEGOTIATION"
        assert len(deal.comments) == 1
        assert "Client requested revised security architecture review." in deal.comments[0].comment

        md = deal.to_markdown()
        assert "# CRM Deal: Enterprise Cloud Migration" in md
        assert "**Amount:** 45000.00 USD" in md
        assert "Timeline Comments:" in md

    asyncio.run(run_test())


def test_fetch_contacts(webhook_url):
    async def run_test():
        contacts_data = {
            "result": [
                {
                    "ID": "7",
                    "NAME": "Alice",
                    "LAST_NAME": "Henderson",
                    "POST": "Chief Technology Officer",
                    "PHONE": [{"VALUE": "+1-555-0199", "VALUE_TYPE": "WORK"}],
                    "EMAIL": [{"VALUE": "alice@acme.org", "VALUE_TYPE": "WORK"}],
                    "COMMENTS": "Key decision maker for enterprise deals.",
                    "DATE_CREATE": "2026-09-01T08:00:00+03:00",
                    "DATE_MODIFY": "2026-10-10T09:15:00+03:00",
                }
            ]
        }

        mock_session = MagicMock()

        def mock_post(url, json=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=contacts_data)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.post.side_effect = mock_post
        mock_session.closed = False

        connector = Bitrix24Connector(webhook_url=webhook_url, session=mock_session)

        contacts = await connector.fetch_contacts(include_comments=False)
        assert len(contacts) == 1
        contact = contacts[0]
        assert contact.full_name == "Alice Henderson"
        assert contact.phone == "+1-555-0199"
        assert contact.email == "alice@acme.org"

        md = contact.to_markdown()
        assert "# CRM Contact: Alice Henderson" in md
        assert "**Job Title:** Chief Technology Officer" in md
        assert "**Email:** alice@acme.org" in md

    asyncio.run(run_test())


def test_fetch_companies(webhook_url):
    async def run_test():
        companies_data = {
            "result": [
                {
                    "ID": "42",
                    "TITLE": "Acme Global Dynamics",
                    "COMPANY_TYPE": "CUSTOMER",
                    "INDUSTRY": "Software & AI",
                    "REVENUE": "1250000.00",
                    "CURRENCY_ID": "USD",
                    "PHONE": [{"VALUE": "+1-800-555-0100"}],
                    "EMAIL": [{"VALUE": "info@acmeglobal.com"}],
                    "COMMENTS": "Leading global logistics platform provider.",
                    "DATE_CREATE": "2026-01-10T10:00:00+03:00",
                    "DATE_MODIFY": "2026-10-15T12:00:00+03:00",
                }
            ]
        }

        mock_session = MagicMock()

        def mock_post(url, json=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=companies_data)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.post.side_effect = mock_post
        mock_session.closed = False

        connector = Bitrix24Connector(webhook_url=webhook_url, session=mock_session)

        companies = await connector.fetch_companies(include_comments=False)
        assert len(companies) == 1
        comp = companies[0]
        assert comp.title == "Acme Global Dynamics"
        assert comp.revenue == 1250000.0
        assert comp.industry == "Software & AI"

        md = comp.to_markdown()
        assert "# CRM Company: Acme Global Dynamics" in md
        assert "**Annual Revenue:** 1250000.00 USD" in md

    asyncio.run(run_test())


def test_to_documents(webhook_url):
    async def run_test():
        mock_session = MagicMock()

        def mock_post(url, json=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "crm.deal.list" in url:
                mock_resp.json = AsyncMock(
                    return_value={
                        "result": [{"ID": "1", "TITLE": "Seed Deal", "STAGE_ID": "WON", "OPPORTUNITY": 5000}]
                    }
                )
            elif "crm.contact.list" in url:
                mock_resp.json = AsyncMock(return_value={"result": [{"ID": "2", "NAME": "Bob", "LAST_NAME": "Stone"}]})
            elif "crm.company.list" in url:
                mock_resp.json = AsyncMock(return_value={"result": [{"ID": "3", "TITLE": "Stone Corp"}]})
            else:
                mock_resp.json = AsyncMock(return_value={"result": []})

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.post.side_effect = mock_post
        mock_session.closed = False

        connector = Bitrix24Connector(webhook_url=webhook_url, session=mock_session)

        docs = await connector.to_documents(
            crm_types=["deal", "contact", "company"],
            include_comments=False,
        )
        assert len(docs) == 3
        assert any("# CRM Deal: Seed Deal" in doc for doc in docs)
        assert any("# CRM Contact: Bob Stone" in doc for doc in docs)
        assert any("# CRM Company: Stone Corp" in doc for doc in docs)

    asyncio.run(run_test())


def test_auth_and_rate_limits(webhook_url):
    async def run_test():
        # 401 Unauthorized
        mock_session_401 = MagicMock()
        mock_resp_401 = MagicMock()
        mock_resp_401.status = 401
        mock_resp_401.json = AsyncMock(return_value={"error": "INVALID_CREDENTIALS"})
        cm_401 = MagicMock()
        cm_401.__aenter__ = AsyncMock(return_value=mock_resp_401)
        cm_401.__aexit__ = AsyncMock(return_value=None)
        mock_session_401.post.return_value = cm_401
        mock_session_401.closed = False

        connector_401 = Bitrix24Connector(webhook_url=webhook_url, session=mock_session_401)
        with pytest.raises(PermissionError, match="Bitrix24 authentication failed"):
            await connector_401.fetch_deals()

        # 503 Query rate limit exceeded
        mock_session_503 = MagicMock()
        mock_resp_503 = MagicMock()
        mock_resp_503.status = 503
        cm_503 = MagicMock()
        cm_503.__aenter__ = AsyncMock(return_value=mock_resp_503)
        cm_503.__aexit__ = AsyncMock(return_value=None)
        mock_session_503.post.return_value = cm_503
        mock_session_503.closed = False

        connector_503 = Bitrix24Connector(webhook_url=webhook_url, session=mock_session_503)
        with pytest.raises(RuntimeError, match="Bitrix24 query rate limit exceeded"):
            await connector_503.fetch_deals()

    asyncio.run(run_test())
