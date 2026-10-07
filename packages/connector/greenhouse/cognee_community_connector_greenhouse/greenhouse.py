"""DLT source for Greenhouse recruiting jobs, posts, and scorecards.

Fetches Greenhouse recruiting job openings, descriptions, and candidate evaluation
scorecards, formatting them as structured markdown documents for cognee's
knowledge graph ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), Greenhouse entities are ingested as
*normal documents*: the source declares ``cognee_document_source = "greenhouse"``,
so ``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "greenhouse"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each record flows
through the standard cognify entity-extraction pipeline — extracting job roles,
skills, evaluation criteria, and hiring insights into the knowledge graph.

Privacy / Candidate Personal Data Safeguard:
Interview feedback and scorecards contain candidate personal data and sensitive
evaluations. In accordance with the issue specification, interview scorecards
are strictly gated behind an explicit opt-in parameter (``include_interview_feedback=False``
by default). Furthermore, candidate contact details (phone, email, home address)
are stripped to ensure privacy compliance.

The source defaults to a full snapshot: ``write_disposition="replace"`` rewrites
staging with currently active jobs and authorized scorecards. Closed or deleted
items drop out of the snapshot, allowing cognee's ``orphan_cleanup`` to remove stale
nodes from the knowledge graph and vector stores.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("greenhouse_connector")

GREENHOUSE_TABLE_NAME = "greenhouse_records"
GREENHOUSE_SOURCE_NAME = "greenhouse"

_BASE_URL = "https://harvest.greenhouse.io/v1"
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0


def _get_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Calculate exponential retry delay honoring Retry-After headers."""
    if response is not None and "retry-after" in response.headers:
        try:
            return float(response.headers["retry-after"])
        except (ValueError, TypeError):
            pass
    return _BASE_BACKOFF * (2**attempt)


def _strip_html(text: str) -> str:
    """Strip basic HTML tags from job post content."""
    clean = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", clean).strip()


class GreenhouseClient:
    """Synchronous HTTP client for Greenhouse Harvest REST API v1/v2."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        token = (
            api_key
            or os.getenv("GREENHOUSE_HARVEST_API_KEY")
            or os.getenv("GREENHOUSE_API_KEY")
            or os.getenv("GREENHOUSE_ACCESS_TOKEN")
        )
        if not token:
            raise ValueError(
                "Greenhouse Harvest API key required. Pass api_key or set "
                "GREENHOUSE_HARVEST_API_KEY / GREENHOUSE_API_KEY."
            )
        self.api_key = token.strip()
        self.base_url = base_url.rstrip("/")

        # Harvest API accepts Basic auth with the API key as the username
        encoded_auth = base64.b64encode(f"{self.api_key}:".encode()).decode()
        headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-greenhouse/0.1.0",
        }
        self.client = httpx.Client(
            headers=headers,
            timeout=30.0,
            transport=transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}" if path.startswith("/") else path
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.client.request(method, url, params=params)
                if resp.status_code in (429, 500, 502, 503, 504):
                    delay = _get_retry_delay(resp, attempt)
                    logger.warning(
                        "Greenhouse API returned %s, retrying in %.2fs",
                        resp.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.NetworkError) as err:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = _get_retry_delay(None, attempt)
                logger.warning(
                    "Network error calling %s (%s), retrying in %.2fs",
                    url,
                    err,
                    delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"Exceeded max retries calling Greenhouse API: {url}")

    def list_jobs(
        self,
        updated_after: str | None = None,
        created_after: str | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch page of jobs from /jobs."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if updated_after:
            params["updated_after"] = updated_after
        if created_after:
            params["created_after"] = created_after
        if status:
            params["status"] = status

        data = self._request("GET", "/jobs", params=params)
        return data if isinstance(data, list) else []

    def list_job_posts(self, job_id: int | str) -> list[dict[str, Any]]:
        """Fetch job posts / descriptions for a given job."""
        data = self._request("GET", f"/jobs/{job_id}/job_posts")
        return data if isinstance(data, list) else []

    def list_scorecards(
        self,
        job_id: int | str | None = None,
        updated_after: str | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch candidate interview scorecards from /scorecards."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if job_id:
            params["job_id"] = job_id
        if updated_after:
            params["updated_after"] = updated_after

        data = self._request("GET", "/scorecards", params=params)
        return data if isinstance(data, list) else []

    def close(self) -> None:
        """Close underlying HTTP client."""
        self.client.close()


def _job_to_document(
    job: dict[str, Any],
    job_posts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert Greenhouse job and its post descriptions into a structured markdown document."""
    job_id = job.get("id", "unknown")
    name = job.get("name", "Untitled Job")
    status = job.get("status", "open")
    req_id = job.get("requisition_id") or "N/A"
    created_at = job.get("created_at", "N/A")
    updated_at = job.get("updated_at", "N/A")

    departments = [d.get("name", "") for d in job.get("departments", []) if d.get("name")]
    dept_str = ", ".join(departments) if departments else "General"

    offices = [o.get("name", "") for o in job.get("offices", []) if o.get("name")]
    office_str = ", ".join(offices) if offices else "Remote / Flexible"

    notes = (job.get("notes") or "").strip()

    doc_lines = [
        f"# Greenhouse Job: {name}",
        "",
        f"- **Job ID**: {job_id}",
        f"- **Requisition ID**: {req_id}",
        f"- **Status**: {status}",
        f"- **Department**: {dept_str}",
        f"- **Office / Location**: {office_str}",
        f"- **Created At**: {created_at}",
        f"- **Updated At**: {updated_at}",
    ]

    if notes:
        doc_lines.extend(["", "## Internal Notes & Requirements", "", notes])

    if job_posts:
        doc_lines.extend(["", "## Public Job Post & Description"])
        for post in job_posts:
            p_title = post.get("title") or name
            content_html = post.get("content") or ""
            clean_content = _strip_html(content_html) if content_html else ""
            if clean_content:
                doc_lines.extend(["", f"### {p_title}", "", clean_content])

    full_text = "\n".join(doc_lines)
    content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

    return {
        "id": f"greenhouse:job:{job_id}",
        "data_id": f"greenhouse:{content_hash}",
        "name": f"Job: {name} ({dept_str})",
        "text": full_text,
        "type": "job",
        "status": status,
        "updated_at": updated_at,
        "external_metadata": {
            "source": GREENHOUSE_SOURCE_NAME,
            "record_type": "job",
            "job_id": job_id,
            "department": dept_str,
        },
    }


def _scorecard_to_document(scorecard: dict[str, Any]) -> dict[str, Any]:
    """Convert interview scorecard into a sanitized markdown document (excluding candidate PII)."""
    sc_id = scorecard.get("id", "unknown")
    interview_name = scorecard.get("interview") or "Interview Assessment"
    recommendation = scorecard.get("overall_recommendation") or "neutral"
    submitted_at = scorecard.get("submitted_at") or scorecard.get("created_at") or "N/A"
    updated_at = scorecard.get("updated_at") or submitted_at

    ratings = scorecard.get("ratings") or {}
    questions = scorecard.get("questions") or []

    doc_lines = [
        f"# Greenhouse Scorecard: {interview_name}",
        "",
        f"- **Scorecard ID**: {sc_id}",
        f"- **Overall Recommendation**: {recommendation}",
        f"- **Submitted At**: {submitted_at}",
    ]

    if isinstance(ratings, dict) and ratings:
        doc_lines.extend(["", "## Competency Ratings"])
        for comp, rating in ratings.items():
            doc_lines.append(f"- **{comp}**: {rating}")

    if questions:
        doc_lines.extend(["", "## Interview Evaluation Questions & Feedback"])
        for q in questions:
            q_text = q.get("question") or "Question"
            answer = q.get("answer") or "No notes provided"
            doc_lines.append(f"### Q: {q_text}")
            doc_lines.append(f"{answer}")
            doc_lines.append("")

    full_text = "\n".join(doc_lines).strip()
    content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

    return {
        "id": f"greenhouse:scorecard:{sc_id}",
        "data_id": f"greenhouse:{content_hash}",
        "name": f"Scorecard: {interview_name} ({recommendation})",
        "text": full_text,
        "type": "scorecard",
        "recommendation": recommendation,
        "updated_at": updated_at,
        "external_metadata": {
            "source": GREENHOUSE_SOURCE_NAME,
            "record_type": "scorecard",
            "scorecard_id": sc_id,
        },
    }


def greenhouse_source(
    api_key: str | None = None,
    updated_after: str | None = None,
    created_after: str | None = None,
    job_status: str | None = "open",
    include_job_posts: bool = True,
    include_interview_feedback: bool = False,
    client: GreenhouseClient | None = None,
):
    """Create a dlt source yielding Greenhouse jobs and scorecards as markdown documents.

    Args:
        api_key: Greenhouse Harvest API key.
        updated_after: ISO8601 timestamp cutoff for updated records.
        created_after: Optional ISO8601 creation cutoff.
        job_status: Filter jobs by status ('open', 'closed').
        include_job_posts: Whether to fetch public job posts/descriptions.
        include_interview_feedback: Explicit opt-in flag to ingest interview scorecards.
            Defaults to False to safeguard candidate personal data.
        client: Optional preconfigured GreenhouseClient instance.
    """
    import dlt

    gh_client = client or GreenhouseClient(api_key=api_key)

    @dlt.resource(
        name=GREENHOUSE_TABLE_NAME,
        write_disposition="replace",
    )
    def records_resource():
        state = dlt.current.resource_state()
        watermark = state.get("last_updated_after")
        effective_updated = updated_after or watermark

        max_seen_updated = effective_updated

        # 1. Ingest Jobs
        page = 1
        while True:
            jobs = gh_client.list_jobs(
                updated_after=effective_updated,
                created_after=created_after,
                status=job_status,
                page=page,
                per_page=100,
            )
            if not jobs:
                break

            for job in jobs:
                j_up = job.get("updated_at")
                if j_up and (max_seen_updated is None or j_up > max_seen_updated):
                    max_seen_updated = j_up

                job_posts: list[dict[str, Any]] = []
                if include_job_posts:
                    j_id = job.get("id")
                    try:
                        job_posts = gh_client.list_job_posts(j_id)
                    except Exception as err:
                        logger.warning("Could not fetch job posts for job %s: %s", j_id, err)

                yield _job_to_document(job, job_posts)

            if len(jobs) < 100:
                break
            page += 1

        # 2. Ingest Scorecards (Strictly gated behind explicit opt-in)
        if include_interview_feedback:
            sc_page = 1
            while True:
                scorecards = gh_client.list_scorecards(
                    updated_after=effective_updated,
                    page=sc_page,
                    per_page=100,
                )
                if not scorecards:
                    break

                for sc in scorecards:
                    sc_up = sc.get("updated_at")
                    if sc_up and (max_seen_updated is None or sc_up > max_seen_updated):
                        max_seen_updated = sc_up

                    yield _scorecard_to_document(sc)

                if len(scorecards) < 100:
                    break
                sc_page += 1

        if max_seen_updated:
            state["last_updated_after"] = max_seen_updated

    @dlt.source(name=GREENHOUSE_SOURCE_NAME)
    def source():
        return records_resource

    created_source = source()
    created_source.cognee_document_source = GREENHOUSE_SOURCE_NAME
    setattr(created_source, DOCUMENT_SOURCE_ATTR, GREENHOUSE_SOURCE_NAME)
    return created_source
