import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp


@dataclass
class JotformSubmission:
    id: str
    form_id: str
    form_title: str
    created_at: str
    status: str
    answers: Dict[str, str] = field(default_factory=dict)

    def to_markdown(self) -> str:
        lines = [
            f"# Form Submission: {self.form_title}",
            f"- **Submission ID:** {self.id}",
            f"- **Form ID:** {self.form_id}",
            f"- **Submitted At:** {self.created_at}",
            f"- **Status:** {self.status}",
            "\n### Submitted Responses:",
        ]
        for question_label, answer_text in self.answers.items():
            clean_ans = re.sub(r"<[^>]+>", "", str(answer_text)).strip()
            clean_ans = re.sub(r"\s+", " ", clean_ans)
            if clean_ans:
                lines.append(f"- **{question_label}:** {clean_ans}")
        return "\n".join(lines)


@dataclass
class JotformForm:
    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    submission_count: int = 0
    submissions: List[JotformSubmission] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Form Overview: {self.title}",
            f"- **Form ID:** {self.id}",
            f"- **Status:** {self.status}",
            f"- **Total Submissions:** {self.submission_count}",
            f"- **Created:** {self.created_at}",
            f"- **Updated:** {self.updated_at}",
        ]
        return "\n".join(lines)


class JotformConnector:
    """Cognee community connector for the Jotform online forms REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.jotform.com",
        form_ids: Optional[List[str]] = None,
        exclude_payment_fields: bool = True,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.form_ids = form_ids
        self.exclude_payment_fields = exclude_payment_fields
        self._session = session
        self._owns_session = False
        self._question_cache: Dict[str, Dict[str, str]] = {}

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
            "APIKEY": self.api_key,
            "Accept": "application/json",
            "User-Agent": "Cognee-Connector-Jotform/0.1.0",
        }

        async with session.get(url, headers=headers, params=params or {}) as response:
            if response.status == 401:
                raise PermissionError("Jotform authentication failed: Invalid or expired API key")
            if response.status == 403:
                raise PermissionError("Jotform authorization failed: Access denied to this form")
            if response.status == 429:
                raise RuntimeError("Jotform daily API rate limit exceeded (HTTP 429)")
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"Jotform API HTTP {response.status} error: {text}")

            payload = await response.json()
            if isinstance(payload, dict):
                response_code = payload.get("responseCode", 200)
                if response_code in (401, 403):
                    raise PermissionError(f"Jotform API error: {payload.get('message', 'Unauthorized')}")
                return payload.get("content", {})
            return payload

    async def fetch_questions(self, form_id: str) -> Dict[str, str]:
        """Fetch form schema questions, mapping question ID to question text."""
        if form_id in self._question_cache:
            return self._question_cache[form_id]

        try:
            content = await self._get(f"/form/{form_id}/questions")
        except Exception:
            return {}

        question_map: Dict[str, str] = {}
        if isinstance(content, dict):
            for q_id, q_info in content.items():
                if not isinstance(q_info, dict):
                    continue
                # Skip payment widgets if configured
                q_type = q_info.get("type", "")
                if self.exclude_payment_fields and "payment" in q_type.lower():
                    continue
                label = q_info.get("text") or q_info.get("label") or f"Field {q_id}"
                clean_label = re.sub(r"<[^>]+>", "", label).strip()
                question_map[str(q_id)] = clean_label or f"Field {q_id}"

        self._question_cache[form_id] = question_map
        return question_map

    async def fetch_forms(self, limit: int = 50) -> List[JotformForm]:
        """Fetch available forms from Jotform account."""
        content = await self._get("/user/forms", params={"limit": limit, "orderby": "updated_at"})
        forms = []
        if isinstance(content, list):
            for it in content:
                f_id = str(it.get("id", ""))
                if self.form_ids and f_id not in self.form_ids:
                    continue
                form = JotformForm(
                    id=f_id,
                    title=it.get("title", f"Form #{f_id}"),
                    status=it.get("status", "ENABLED"),
                    created_at=it.get("created_at", ""),
                    updated_at=it.get("updated_at", ""),
                    submission_count=int(it.get("count", 0) or 0),
                )
                forms.append(form)
        return forms

    async def fetch_submissions(
        self,
        form_id: str,
        form_title: str = "Form",
        limit: int = 50,
    ) -> List[JotformSubmission]:
        """Fetch submissions for a given form with human-readable question labels."""
        question_map = await self.fetch_questions(form_id)
        content = await self._get(
            f"/form/{form_id}/submissions",
            params={"limit": limit, "orderby": "created_at"},
        )

        submissions = []
        if isinstance(content, list):
            for sub in content:
                sub_id = str(sub.get("id", ""))
                answers_raw = sub.get("answers", {})
                resolved_answers: Dict[str, str] = {}

                if isinstance(answers_raw, dict):
                    for q_id, ans_info in answers_raw.items():
                        if not isinstance(ans_info, dict):
                            continue
                        label = question_map.get(str(q_id)) or ans_info.get("text") or f"Field {q_id}"
                        answer_val = ans_info.get("answer")

                        # Format complex answers (dicts like first/last name or address)
                        if isinstance(answer_val, dict):
                            parts = [f"{k}: {v}" for k, v in answer_val.items() if v]
                            formatted_answer = ", ".join(parts)
                        elif isinstance(answer_val, list):
                            formatted_answer = ", ".join(str(v) for v in answer_val if v)
                        elif answer_val is not None:
                            formatted_answer = str(answer_val)
                        else:
                            formatted_answer = ""

                        if formatted_answer:
                            resolved_answers[label] = formatted_answer

                submission = JotformSubmission(
                    id=sub_id,
                    form_id=form_id,
                    form_title=form_title,
                    created_at=sub.get("created_at", ""),
                    status=sub.get("status", "ACTIVE"),
                    answers=resolved_answers,
                )
                submissions.append(submission)

        return submissions

    async def to_documents(self, limit_per_form: int = 50) -> List[str]:
        """Convert forms and submissions to Markdown documents for Cognee memory ingestion."""
        forms = await self.fetch_forms()
        docs = []

        for form in forms:
            docs.append(form.to_markdown())
            submissions = await self.fetch_submissions(
                form_id=form.id,
                form_title=form.title,
                limit=limit_per_form,
            )
            for sub in submissions:
                docs.append(sub.to_markdown())

        return docs
