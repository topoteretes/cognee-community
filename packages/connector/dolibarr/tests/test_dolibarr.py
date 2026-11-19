import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cognee_community_connector_dolibarr.dolibarr import (
    DolibarrConnector,
    DolibarrThirdParty,
    DolibarrContact,
    DolibarrProposal,
    DolibarrTicket,
)


@pytest.fixture
def dolibarr_config():
    return {
        "base_url": "https://erp.example.com",
        "api_key": "sample_dolapikey_secret",
    }


def test_fetch_thirdparties(dolibarr_config):
    async def run_test():
        parties_data = [
            {
                "id": "12",
                "name": "OmniCorp Global",
                "name_alias": "OmniCorp",
                "email": "contact@omnicorp.com",
                "phone": "+1-888-555-0144",
                "address": "100 Tech Blvd",
                "town": "Metropolis",
                "zip": "90210",
                "status": "1",
                "tms": "1730000000",
                "note_public": "<p>Strategic supply chain partner since 2024.</p>",
                "note_private": "<p>Pending contract renewal negotiation.</p>",
            }
        ]

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=parties_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        # 1. With private notes excluded (default)
        connector = DolibarrConnector(
            base_url=dolibarr_config["base_url"],
            api_key=dolibarr_config["api_key"],
            include_private_notes=False,
            session=mock_session,
        )
        parties = await connector.fetch_thirdparties()
        assert len(parties) == 1
        p = parties[0]
        assert p.id == "12"
        assert p.name == "OmniCorp Global"
        assert p.status == "Active"

        md_pub = p.to_markdown(include_private_notes=False)
        assert "# Third-Party (Company/Client): OmniCorp Global" in md_pub
        assert "Strategic supply chain partner since 2024." in md_pub
        assert "Private Internal Notes" not in md_pub

        # 2. With private notes included
        md_priv = p.to_markdown(include_private_notes=True)
        assert "Private Internal Notes [RESTRICTED]:" in md_priv
        assert "Pending contract renewal negotiation." in md_priv

    asyncio.run(run_test())


def test_fetch_contacts(dolibarr_config):
    async def run_test():
        contacts_data = [
            {
                "id": "55",
                "firstname": "Sarah",
                "lastname": "Connor",
                "socid": "12",
                "phone": "+1-555-0188",
                "email": "sconnor@omnicorp.com",
                "post": "Lead Engineer",
                "note_public": "Specialized in automation infrastructure.",
            }
        ]

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=contacts_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = DolibarrConnector(
            base_url=dolibarr_config["base_url"],
            api_key=dolibarr_config["api_key"],
            session=mock_session,
        )
        contacts = await connector.fetch_contacts()
        assert len(contacts) == 1
        c = contacts[0]
        assert c.full_name == "Sarah Connor"
        assert c.socid == "12"
        assert c.email == "sconnor@omnicorp.com"

        md = c.to_markdown()
        assert "# CRM Contact: Sarah Connor" in md
        assert "**Company (SocID):** 12" in md
        assert "**Job Title:** Lead Engineer" in md

    asyncio.run(run_test())


def test_fetch_proposals(dolibarr_config):
    async def run_test():
        proposals_data = [
            {
                "id": "77",
                "ref": "PR2610-0042",
                "socid": "12",
                "total_ht": 15000.0,
                "total_ttc": 18000.0,
                "statut": "1",
                "date_validation": "2026-10-22",
            }
        ]

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=proposals_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = DolibarrConnector(
            base_url=dolibarr_config["base_url"],
            api_key=dolibarr_config["api_key"],
            session=mock_session,
        )
        proposals = await connector.fetch_proposals()
        assert len(proposals) == 1
        prop = proposals[0]
        assert prop.ref == "PR2610-0042"
        assert prop.total_ht == 15000.0
        assert prop.status_label == "Open / Validated"

        md = prop.to_markdown()
        assert "# Commercial Proposal: PR2610-0042" in md
        assert "**Amount (Excl. Tax):** 15000.00" in md

    asyncio.run(run_test())


def test_fetch_tickets(dolibarr_config):
    async def run_test():
        tickets_data = [
            {
                "id": "89",
                "ref": "TIK-2026-09",
                "track_id": "TRK8820",
                "subject": "VPN Gateway Routing Latency",
                "message": "<p>Network latency spikes observed during daily sync.</p>",
                "type_label": "Technical Issue",
                "category_label": "Infrastructure",
                "severity_label": "High",
                "status_label": "In Progress",
            }
        ]

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=tickets_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = DolibarrConnector(
            base_url=dolibarr_config["base_url"],
            api_key=dolibarr_config["api_key"],
            session=mock_session,
        )
        tickets = await connector.fetch_tickets()
        assert len(tickets) == 1
        t = tickets[0]
        assert t.ref == "TIK-2026-09"
        assert t.subject == "VPN Gateway Routing Latency"
        assert t.severity_label == "High"

        md = t.to_markdown()
        assert "# Support Ticket: [TIK-2026-09] VPN Gateway Routing Latency" in md
        assert "Network latency spikes observed during daily sync." in md

    asyncio.run(run_test())


def test_404_empty_list_handling(dolibarr_config):
    async def run_test():
        # Dolibarr returns 404 when 0 records match
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.json = AsyncMock(return_value={"error": {"code": 404, "message": "Not Found: No tickets found"}})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = DolibarrConnector(
            base_url=dolibarr_config["base_url"],
            api_key=dolibarr_config["api_key"],
            session=mock_session,
        )
        tickets = await connector.fetch_tickets()
        assert tickets == []

    asyncio.run(run_test())


def test_auth_error_handling(dolibarr_config):
    async def run_test():
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 401
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = DolibarrConnector(
            base_url=dolibarr_config["base_url"],
            api_key=dolibarr_config["api_key"],
            session=mock_session,
        )
        with pytest.raises(PermissionError, match="Dolibarr authentication failed: Invalid or expired DOLAPIKEY"):
            await connector.fetch_thirdparties()

    asyncio.run(run_test())


def test_to_documents(dolibarr_config):
    async def run_test():
        mock_session = MagicMock()

        def mock_get(url, headers=None, params=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "thirdparties" in url:
                mock_resp.json = AsyncMock(return_value=[{"id": "1", "name": "Alpha Corp"}])
            elif "contacts" in url:
                mock_resp.json = AsyncMock(return_value=[{"id": "2", "firstname": "John", "lastname": "Doe"}])
            elif "proposals" in url:
                mock_resp.json = AsyncMock(return_value=[{"id": "3", "ref": "PROP-01", "total_ht": 1000}])
            elif "tickets" in url:
                mock_resp.json = AsyncMock(
                    return_value=[{"id": "4", "ref": "TIK-01", "subject": "Bug", "message": "Crash"}]
                )
            else:
                mock_resp.json = AsyncMock(return_value=[])

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = DolibarrConnector(
            base_url=dolibarr_config["base_url"],
            api_key=dolibarr_config["api_key"],
            session=mock_session,
        )
        docs = await connector.to_documents()
        assert len(docs) == 4
        assert any("# Third-Party (Company/Client): Alpha Corp" in d for d in docs)
        assert any("# CRM Contact: John Doe" in d for d in docs)
        assert any("# Commercial Proposal: PROP-01" in d for d in docs)
        assert any("# Support Ticket: [TIK-01] Bug" in d for d in docs)

    asyncio.run(run_test())
