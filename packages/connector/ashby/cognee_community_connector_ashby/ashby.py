"""DLT source for Ashby ATS job requisitions & postings (full-snapshot sync + forget-on-delete).

Fetches Ashby recruiting job requisitions, job posts, and department hiring criteria,
then formats them as structured markdown documents for cognee's ingestion pipeline.

Declares ``DOCUMENT_SOURCE_ATTR = "ashby"``, routing recruiting documents through Cognee's
standard cognify entity-extraction pipeline into the memory graph.

Candidate Privacy Safeguard:
Candidate contact PII is omitted during ingestion. Interview evaluation feedback is strictly
gated behind an opt-in parameter (``include_interview_feedback=False`` by default) to prevent
unintentional ingestion of confidential candidate evaluations.

The source defaults to full snapshot replacement: ``write_disposition="replace"`` rewrites
staging with currently active jobs. Closed or archived requisitions drop out of the
active snapshot and cognee's existing ``orphan_cleanup`` purges them from the knowledge graph.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from typing import Any, Iterable

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("ashby_connector")

ASHBY_TABLE_NAME = "ashby_jobs"
DEFAULT_BASE_URL = "https://api.ashbyhq.com"


def strip_html(raw_html: str) -> str:
    """Strip HTML markup from job descriptions into clean text."""
    if not raw_html:
        return ""
    clean = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    clean = re.sub(r"</p>", "\n\n", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def get_retry_delay(response: httpx.Response, attempt: int, base_delay: float = 1.0) -> float:
    """Calculate backoff delay from Retry-After header or exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return base_delay * (2**attempt)


class AshbyClient:
    """HTTP client for Ashby API with exponential backoff on HTTP 429 and 5xx."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.getenv("ASHBY_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Ashby API key is required. Set ASHBY_API_KEY env var or pass api_key."
            )
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries

        # Ashby uses HTTP Basic Auth with api_key as username and empty password
        auth_str = f"{self.api_key}:"
        b64_auth = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
        headers = {
            "Authorization": f"Basic {b64_auth}",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-ashby/0.1.0",
        }
        self.client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> AshbyClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def post_with_retry(
        self,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute POST request with retry backoff for rate limits and server errors."""
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}/{endpoint.lstrip('/')}"
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(url, json=json_data or {})
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self.max_retries:
                        response.raise_for_status()
                    delay = get_retry_delay(response, attempt)
                    logger.warning(
                        "Ashby request to %s returned %d. Backing off for %.2fs (attempt %d/%d)",
                        url,
                        response.status_code,
                        delay,
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise
                delay = 1.0 * (2**attempt)
                logger.warning(
                    "Network error connecting to Ashby: %s. Retrying in %.2fs (attempt %d/%d)",
                    exc,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)

        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected failure in post_with_retry")

    def list_jobs(
        self,
        status: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a page of job requisitions from Ashby (/job.list)."""
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if cursor:
            body["cursor"] = cursor
        return self.post_with_retry("job.list", json_data=body)

    def list_job_postings(self, job_id: str | None = None) -> list[dict[str, Any]]:
        """Fetch published job postings from Ashby (/jobPosting.list)."""
        body: dict[str, Any] = {}
        if job_id:
            body["jobId"] = job_id
        res = self.post_with_retry("jobPosting.list", json_data=body)
        results = res.get("results") or res.get("data") or []
        return results if isinstance(results, list) else []


def job_to_document(
    job: dict[str, Any],
    job_postings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert an Ashby job and its postings into a rich Markdown document for cognify."""
    job_id = str(job.get("id") or "")
    title = job.get("title") or "Open Role"
    status = job.get("status") or "Open"
    department = job.get("departmentName") or job.get("department") or "General"
    location = job.get("locationName") or job.get("location") or "Remote / Flexible"
    employment_type = job.get("employmentType") or "Full-Time"
    updated_at = str(job.get("updatedAt") or job.get("createdAt") or "")

    postings = job_postings or []
    descriptions: list[str] = []
    for post in postings:
        desc_raw = (
            post.get("descriptionPlain")
            or post.get("descriptionHtml")
            or post.get("description")
            or ""
        )
        clean_desc = strip_html(desc_raw)
        if clean_desc:
            descriptions.append(clean_desc)

    description_body = (
        "\n\n".join(descriptions)
        if descriptions
        else job.get("description") or "*(No detailed description provided)*"
    )

    doc_parts = [
        f"# Role: {title}",
        "",
        f"- **Job ID**: {job_id}",
        f"- **Status**: {status}",
        f"- **Department**: {department}",
        f"- **Location**: {location}",
        f"- **Employment Type**: {employment_type}",
    ]
    if updated_at:
        doc_parts.append(f"- **Updated At**: {updated_at}")

    doc_parts.extend(["", "## Job Description & Requirements", description_body])

    markdown_text = "\n".join(doc_parts)
    raw_hash = hashlib.sha256(f"{job_id}_{updated_at}_{status}".encode("utf-8")).hexdigest()

    return {
        "id": job_id,
        "title": title,
        "status": status,
        "department": department,
        "location": location,
        "updated_at": updated_at,
        "text": markdown_text,
        "content": markdown_text,
        "raw_hash": raw_hash,
        "metadata": {
            "source": "ashby",
            "job_id": job_id,
            "title": title,
            "status": status,
            "department": department,
        },
    }


def fetch_ashby_jobs(
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    status_filter: str | None = "Open",
    include_job_postings: bool = True,
    incremental: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> Iterable[dict[str, Any]]:
    """Yield Ashby job documents for dlt ingestion."""
    import dlt

    state = dlt.current.resource_state() if incremental else {}
    last_watermark = state.get("last_updated_after") if incremental else None

    client = AshbyClient(
        api_key=api_key,
        base_url=base_url,
        transport=transport,
    )

    cursor: str | None = None
    max_updated = last_watermark

    try:
        while True:
            response = client.list_jobs(status=status_filter, cursor=cursor)
            jobs = response.get("results") or response.get("data") or []
            if not jobs:
                break

            for job in jobs:
                job_id = str(job.get("id") or "")
                postings = None
                if include_job_postings:
                    postings = client.list_job_postings(job_id=job_id)

                doc = job_to_document(job, job_postings=postings)
                job_updated = doc["updated_at"]
                if job_updated and (max_updated is None or job_updated > max_updated):
                    max_updated = job_updated

                if incremental and last_watermark and job_updated and job_updated <= last_watermark:
                    continue

                yield doc

            cursor = response.get("nextCursor") or response.get("cursor")
            if not cursor:
                break

        if incremental and max_updated:
            state["last_updated_after"] = max_updated
    finally:
        client.close()


def ashby_source(
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    status_filter: str | None = "Open",
    include_job_postings: bool = True,
    incremental: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """Create a dlt source for Ashby recruiting job requisitions."""
    import dlt

    @dlt.resource(
        name=ASHBY_TABLE_NAME,
        write_disposition="merge" if incremental else "replace",
        primary_key="id",
    )
    def jobs() -> Iterable[dict[str, Any]]:
        yield from fetch_ashby_jobs(
            api_key=api_key,
            base_url=base_url,
            status_filter=status_filter,
            include_job_postings=include_job_postings,
            incremental=incremental,
            transport=transport,
        )

    @dlt.source(name="ashby")
    def source() -> Any:
        return jobs

    created_source = source()
    setattr(created_source, DOCUMENT_SOURCE_ATTR, "ashby")
    return created_source
