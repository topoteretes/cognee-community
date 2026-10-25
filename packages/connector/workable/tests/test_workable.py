import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cognee_community_connector_workable.workable import (
    WorkableConnector,
    WorkableJob,
    WorkableCandidate,
)


@pytest.fixture
def workable_config():
    return {
        "api_key": "test_workable_token_12345",
        "subdomain": "acme-corp",
    }


def test_fetch_jobs_with_candidates(workable_config):
    async def run_test():
        jobs_data = {
            "jobs": [
                {
                    "id": "job_101",
                    "title": "Senior AI Systems Engineer",
                    "shortcode": "ENG01",
                    "state": "published",
                    "department": "Engineering & Core AI",
                    "location": {"city": "San Francisco", "region": "CA", "country": "United States"},
                    "employment_type": "Full-time",
                    "url": "https://acme-corp.workable.com/j/ENG01",
                    "created_at": "2026-10-15T10:00:00Z",
                    "description": "<p>Lead our high-throughput distributed LLM memory graphs.</p>",
                    "requirements": "<p>5+ years with Python, async microservices, and vector DBs.</p>",
                }
            ]
        }

        candidates_data = {
            "candidates": [
                {
                    "id": "cand_501",
                    "name": "Jane Doe",
                    "stage": "interview",
                    "headline": "Staff ML Infrastructure Engineer at CloudScale",
                    "created_at": "2026-10-18T14:00:00Z",
                    "updated_at": "2026-10-21T09:30:00Z",
                }
            ]
        }

        mock_session = MagicMock()

        def mock_get(url, headers=None, params=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "/candidates" in url:
                mock_resp.json = AsyncMock(return_value=candidates_data)
            else:
                mock_resp.json = AsyncMock(return_value=jobs_data)

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = WorkableConnector(
            api_key=workable_config["api_key"],
            subdomain=workable_config["subdomain"],
            include_candidate_pii=False,
            session=mock_session,
        )

        jobs = await connector.fetch_jobs(include_candidates=True)
        assert len(jobs) == 1
        j = jobs[0]
        assert j.id == "job_101"
        assert j.title == "Senior AI Systems Engineer"
        assert j.department == "Engineering & Core AI"
        assert j.location == "San Francisco, CA, United States"
        assert len(j.candidates) == 1
        assert j.candidates[0].candidate_name == "Anonymous Candidate"

        md = j.to_markdown()
        assert "# Job Posting: Senior AI Systems Engineer" in md
        assert "**Department:** Engineering & Core AI" in md
        assert "Lead our high-throughput distributed LLM memory graphs." in md
        assert "Candidate Profile: Anonymous Candidate" in md

    asyncio.run(run_test())


def test_candidate_pii_toggle(workable_config):
    async def run_test():
        candidates_data = {
            "candidates": [
                {
                    "id": "cand_502",
                    "name": "Alexander Pierce",
                    "stage": "offer",
                    "headline": "Principal Architect",
                }
            ]
        }

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=candidates_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        # 1. With PII enabled
        connector_pii = WorkableConnector(
            api_key=workable_config["api_key"],
            subdomain=workable_config["subdomain"],
            include_candidate_pii=True,
            session=mock_session,
        )
        cands_pii = await connector_pii.fetch_candidates_for_job("ENG01")
        assert len(cands_pii) == 1
        assert cands_pii[0].candidate_name == "Alexander Pierce"

    asyncio.run(run_test())


def test_auth_and_rate_limits(workable_config):
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

        connector_401 = WorkableConnector(
            api_key=workable_config["api_key"],
            subdomain=workable_config["subdomain"],
            session=mock_session_401,
        )
        with pytest.raises(PermissionError, match="Workable authentication failed"):
            await connector_401.fetch_jobs()

        # 429 Rate limit
        mock_session_429 = MagicMock()
        mock_resp_429 = MagicMock()
        mock_resp_429.status = 429
        cm_429 = MagicMock()
        cm_429.__aenter__ = AsyncMock(return_value=mock_resp_429)
        cm_429.__aexit__ = AsyncMock(return_value=None)
        mock_session_429.get.return_value = cm_429
        mock_session_429.closed = False

        connector_429 = WorkableConnector(
            api_key=workable_config["api_key"],
            subdomain=workable_config["subdomain"],
            session=mock_session_429,
        )
        with pytest.raises(RuntimeError, match="Workable API rate limit exceeded"):
            await connector_429.fetch_jobs()

    asyncio.run(run_test())


def test_to_documents(workable_config):
    async def run_test():
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "jobs": [
                    {
                        "id": "j_1",
                        "title": "Lead Product Designer",
                        "shortcode": "DES01",
                        "state": "published",
                        "department": "Design",
                        "description": "Craft intuitive experiences.",
                    }
                ]
            }
        )
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = WorkableConnector(
            api_key=workable_config["api_key"],
            subdomain=workable_config["subdomain"],
            session=mock_session,
        )
        docs = await connector.to_documents(include_candidates=False)
        assert len(docs) == 1
        assert "# Job Posting: Lead Product Designer" in docs[0]
        assert "Craft intuitive experiences." in docs[0]

    asyncio.run(run_test())
