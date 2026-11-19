import logging
import time
from collections.abc import Iterator
from typing import Any

import dlt
import httpx
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources import DltSource

logger = logging.getLogger(__name__)

TLDV_SOURCE_NAME = "tldv"
TLDV_API_BASE = "https://api.tldv.io/v1"
_MAX_RETRIES = 3


class TLDVClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = TLDV_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-tldv",
        }

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(_MAX_RETRIES):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                    )
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("Retry-After", 2**attempt))
                        time.sleep(retry_after)
                        continue
                    if resp.status_code == 404:
                        return {}
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403, 404) or attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        return {}

    def get_meetings(
        self,
        limit: int = 50,
        page: int = 1,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "page": page}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date

        res = self._request("GET", "/meetings", params=params)
        return res if isinstance(res, dict) else {"data": res}

    def get_transcript(self, meeting_id: str) -> list[dict[str, Any]]:
        res = self._request("GET", f"/meetings/{meeting_id}/transcript")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data", res.get("transcript", []))
        return []

    def get_notes(self, meeting_id: str) -> dict[str, Any]:
        res = self._request("GET", f"/meetings/{meeting_id}/notes")
        return res if isinstance(res, dict) else {}


def _format_meeting_to_row(
    meeting: dict[str, Any],
    transcript: list[dict[str, Any]] | None = None,
    notes: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    meeting_id = meeting.get("id") or meeting.get("meetingId")
    if not meeting_id:
        return None

    clean_id = str(meeting_id).strip()
    title = meeting.get("title") or meeting.get("name") or f"Meeting {clean_id}"
    organizer_obj = meeting.get("organizer")
    organizer = (
        organizer_obj.get("name") or organizer_obj.get("email")
        if isinstance(organizer_obj, dict)
        else meeting.get("organizerEmail", "Unknown")
    )
    happened_at = meeting.get("happenedAt") or meeting.get("createdAt") or ""
    duration = meeting.get("duration") or 0

    participants = meeting.get("participants", [])
    participant_names = []
    for p in participants:
        if isinstance(p, dict):
            p_name = p.get("name") or p.get("email") or ""
            if p_name:
                participant_names.append(p_name)
        elif isinstance(p, str):
            participant_names.append(p)

    notes_obj = notes or meeting.get("notes") or {}
    summary_text = ""
    action_items: list[str] = []
    if isinstance(notes_obj, dict):
        summary_text = notes_obj.get("summary") or notes_obj.get("overview") or ""
        raw_actions = notes_obj.get("actionItems") or notes_obj.get("actions") or []
        for action in raw_actions:
            if isinstance(action, dict):
                action_items.append(action.get("text") or action.get("title") or str(action))
            elif isinstance(action, str):
                action_items.append(action)
    elif isinstance(notes_obj, str):
        summary_text = notes_obj

    transcript_lines = []
    for entry in transcript or []:
        speaker = entry.get("speaker") or entry.get("speakerName") or "Speaker"
        text = entry.get("text") or entry.get("content") or ""
        if text:
            transcript_lines.append(f"{speaker}: {text}")

    formatted_transcript = "\n".join(transcript_lines)

    body_parts = [
        f"# Meeting: {title}",
        f"- **Organizer:** {organizer}",
    ]
    if participant_names:
        body_parts.append(f"- **Participants:** {', '.join(participant_names)}")
    if happened_at:
        body_parts.append(f"- **Date:** {happened_at}")
    if duration:
        body_parts.append(f"- **Duration:** {duration} seconds")

    if summary_text:
        body_parts.append(f"\n### Executive Summary\n{summary_text}")
    if action_items:
        body_parts.append("\n### Action Items")
        for action in action_items:
            body_parts.append(f"- {action}")
    if formatted_transcript:
        body_parts.append(f"\n### Full Transcript\n{formatted_transcript}")

    full_text = "\n".join(body_parts).strip()

    return {
        "id": f"tldv_meeting_{clean_id}",
        "title": title,
        "text": full_text,
        "url": meeting.get("url") or f"https://app.tldv.io/meetings/{clean_id}",
        "resource_type": "meeting",
        "last_updated": happened_at,
    }


def tldv_source(
    api_key: str,
    base_url: str = TLDV_API_BASE,
    limit: int = 50,
    from_date: str | None = None,
    to_date: str | None = None,
    include_transcripts: bool = True,
    include_notes: bool = True,
    since: str | None = None,
    client: TLDVClient | None = None,
) -> DltSource:
    api_client = client or TLDVClient(api_key=api_key, base_url=base_url)

    @dlt.resource(name="tldv_meetings", write_disposition="replace")
    def tldv_meetings() -> Iterator[dict[str, Any]]:
        page = 1
        count = 0
        effective_from = from_date or since

        while True:
            data = api_client.get_meetings(
                limit=limit,
                page=page,
                from_date=effective_from,
                to_date=to_date,
            )
            meetings = (
                data.get("data", data.get("meetings", [])) if isinstance(data, dict) else data
            )
            if not meetings:
                break

            for meeting in meetings:
                happened_at = meeting.get("happenedAt") or meeting.get("createdAt") or ""
                if since and happened_at and happened_at < since:
                    continue

                meeting_id = str(meeting.get("id") or meeting.get("meetingId") or "")
                transcript = (
                    api_client.get_transcript(meeting_id)
                    if (meeting_id and include_transcripts)
                    else []
                )
                notes = api_client.get_notes(meeting_id) if (meeting_id and include_notes) else {}

                row = _format_meeting_to_row(meeting, transcript=transcript, notes=notes)
                if row:
                    count += 1
                    yield row

            pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
            total_pages = pagination.get("totalPages") or pagination.get("pageCount")
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(meetings) < limit:
                break
            page += 1

        logger.info("tl;dv: synced %d meeting record(s).", count)

    @dlt.source(name=TLDV_SOURCE_NAME)
    def _tldv():
        return tldv_meetings

    source = _tldv()
    setattr(source, DOCUMENT_SOURCE_ATTR, TLDV_SOURCE_NAME)
    return source


__all__ = [
    "DOCUMENT_SOURCE_ATTR",
    "TLDVClient",
    "tldv_source",
]
