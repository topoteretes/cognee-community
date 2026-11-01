import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp


@dataclass
class WorkableCandidate:
    id: str
    job_shortcode: str
    stage: str
    headline: str = ""
    disqualification_reason: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    candidate_name: str = "Anonymous Candidate"

    def to_markdown(self) -> str:
        lines = [
            f"### Candidate Profile: {self.candidate_name}",
            f"- **Candidate ID:** {self.id}",
            f"- **Target Job Shortcode:** {self.job_shortcode}",
            f"- **Hiring Stage:** {self.stage.upper()}",
            f"- **Created:** {self.created_at}",
            f"- **Last Updated:** {self.updated_at}",
        ]
        if self.headline:
            lines.append(f"- **Professional Headline:** {self.headline}")
        if self.disqualification_reason:
            lines.append(f"- **Disqualification Reason:** {self.disqualification_reason}")
        return "\n".join(lines)


@dataclass
class WorkableJob:
    id: str
    title: str
    shortcode: str
    state: str
    department: str = ""
    location: str = ""
    employment_type: str = ""
    url: str = ""
    created_at: str = ""
    description: str = ""
    requirements: str = ""
    candidates: List[WorkableCandidate] = field(default_factory=list)

    def to_markdown(self) -> str:
        clean_desc = re.sub(r"<[^>]+>", "", self.description).strip()
        clean_desc = re.sub(r"\s+", " ", clean_desc)
        clean_req = re.sub(r"<[^>]+>", "", self.requirements).strip()
        clean_req = re.sub(r"\s+", " ", clean_req)

        lines = [
            f"# Job Posting: {self.title}",
            f"- **Job ID:** {self.id}",
            f"- **Shortcode:** {self.shortcode}",
            f"- **Status:** {self.state.upper()}",
            f"- **Department:** {self.department or 'General'}",
            f"- **Location:** {self.location or 'Remote'}",
            f"- **Employment Type:** {self.employment_type or 'Full-Time'}",
            f"- **Created At:** {self.created_at}",
        ]
        if self.url:
            lines.append(f"- **Application URL:** {self.url}")
        if clean_desc:
            lines.append(f"\n### Role Description:\n{clean_desc}")
        if clean_req:
            lines.append(f"\n### Key Requirements:\n{clean_req}")
        if self.candidates:
            lines.append("\n### Candidate Pipeline:")
            for cand in self.candidates:
                lines.append(cand.to_markdown())
        return "\n".join(lines)


class WorkableConnector:
    """Cognee community connector for Workable recruiting and ATS REST API v3."""

    def __init__(
        self,
        api_key: str,
        subdomain: str,
        include_candidate_pii: bool = False,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_key = api_key.strip()
        self.subdomain = subdomain.strip()
        self.include_candidate_pii = include_candidate_pii
        self.base_url = f"https://{self.subdomain}.workable.com/spi/v3"
        self._session = session
        self._owns_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "Cognee-Connector-Workable/0.1.0",
        }

        async with session.get(url, headers=headers, params=params or {}) as response:
            if response.status == 401:
                raise PermissionError("Workable authentication failed: Invalid or expired API token")
            if response.status == 403:
                raise PermissionError("Workable authorization failed: Access denied to recruiting workspace")
            if response.status == 429:
                raise RuntimeError("Workable API rate limit exceeded (HTTP 429)")
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"Workable API HTTP {response.status} error: {text}")

            return await response.json()

    async def fetch_candidates_for_job(self, shortcode: str, limit: int = 50) -> List[WorkableCandidate]:
        """Fetch candidate pipeline for a specific job opening."""
        try:
            data = await self._get(f"/jobs/{shortcode}/candidates", params={"limit": limit})
        except Exception:
            return []

        candidates_raw = data.get("candidates", []) if isinstance(data, dict) else []
        candidates = []
        for it in candidates_raw:
            cand_name = "Anonymous Candidate"
            if self.include_candidate_pii and it.get("name"):
                cand_name = it["name"]

            cand = WorkableCandidate(
                id=str(it.get("id", "")),
                job_shortcode=shortcode,
                stage=it.get("stage", "applied"),
                headline=it.get("headline", ""),
                disqualification_reason=it.get("disqualification_reason"),
                created_at=it.get("created_at", ""),
                updated_at=it.get("updated_at", ""),
                candidate_name=cand_name,
            )
            candidates.append(cand)
        return candidates

    async def fetch_jobs(
        self,
        state: Optional[str] = "published",
        limit: int = 50,
        include_candidates: bool = True,
    ) -> List[WorkableJob]:
        """Fetch job postings with descriptions, requirements, and candidate pipelines."""
        params: Dict[str, Any] = {"limit": limit}
        if state:
            params["state"] = state

        data = await self._get("/jobs", params=params)
        jobs_raw = data.get("jobs", []) if isinstance(data, dict) else []
        jobs = []

        for it in jobs_raw:
            loc = it.get("location") or {}
            loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
            shortcode = str(it.get("shortcode", ""))

            job = WorkableJob(
                id=str(it.get("id", "")),
                title=it.get("title", f"Role #{shortcode}"),
                shortcode=shortcode,
                state=it.get("state", "published"),
                department=it.get("department", ""),
                location=loc_str or "Remote",
                employment_type=it.get("employment_type", "Full-time"),
                url=it.get("url", ""),
                created_at=it.get("created_at", ""),
                description=it.get("description", ""),
                requirements=it.get("requirements", ""),
            )
            if include_candidates and shortcode:
                job.candidates = await self.fetch_candidates_for_job(shortcode, limit=10)
            jobs.append(job)

        return jobs

    async def to_documents(
        self,
        state: Optional[str] = "published",
        limit: int = 50,
        include_candidates: bool = True,
    ) -> List[str]:
        """Convert all job postings and candidate pipelines into Markdown documents for Cognee memory ingestion."""
        jobs = await self.fetch_jobs(state=state, limit=limit, include_candidates=include_candidates)
        return [j.to_markdown() for j in jobs]
