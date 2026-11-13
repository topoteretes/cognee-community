"""Validate the documented meeting shapes and render only selected report content."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from .models import IncompleteSyncError, Selection

_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def meeting_id(value: Any) -> str:
    if not isinstance(value, str) or not _ULID.fullmatch(value):
        raise IncompleteSyncError("Read.ai returned an invalid meeting ULID")
    return value


def _time(value: Any, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IncompleteSyncError(f"Read.ai returned invalid {field}")
    try:
        datetime.fromtimestamp(value / 1000, UTC)
    except (ValueError, OverflowError, OSError) as error:
        raise IncompleteSyncError(f"Read.ai returned out-of-range {field}") from error
    return value


def _text(value: Any, field: str, *, fallback: str = "") -> str:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise IncompleteSyncError(f"Read.ai returned invalid {field}")
    return value.strip()


def _metrics(value: Any) -> Any:
    """Keep numeric metrics and nested numeric objects; null removes a metric."""
    if value is None:
        return None
    if isinstance(value, dict):
        result = {}
        if any(not isinstance(key, str) or not key.strip() for key in value):
            raise IncompleteSyncError("Read.ai returned an invalid metric name")
        for key in sorted(value):
            child = _metrics(value[key])
            if child is not None and child != {}:
                result[key] = child
        return result
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IncompleteSyncError("Read.ai metrics must contain finite numeric values")
    if isinstance(value, float) and not math.isfinite(value):
        raise IncompleteSyncError("Read.ai returned a non-finite metric")
    return value


def _transcript(value: Any, start: int) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise IncompleteSyncError("Read.ai returned an invalid transcript")
    turns = value.get("turns")
    if turns is None:
        turns = []
    if not isinstance(turns, list):
        raise IncompleteSyncError("Read.ai returned invalid transcript turns")
    normalized = []
    for turn in turns:
        if not isinstance(turn, dict):
            raise IncompleteSyncError("Read.ai returned an invalid transcript turn")
        text = _text(turn.get("text"), "transcript turn text")
        stamp = _time(turn.get("start_time_ms"), "transcript timestamp")
        end = _time(turn.get("end_time_ms"), "transcript end timestamp", optional=True)
        if stamp < start or (end is not None and end < stamp):
            raise IncompleteSyncError("Read.ai returned inconsistent transcript timestamps")
        speaker = turn.get("speaker")
        if speaker is None:
            name = "Unknown speaker"
        elif isinstance(speaker, dict):
            name = _text(speaker.get("name"), "speaker name") or "Unknown speaker"
        else:
            raise IncompleteSyncError("Read.ai returned an invalid transcript speaker")
        if text:
            normalized.append({"speaker": name, "text": text, "offset_ms": stamp - start})
    fallback = _text(value.get("text"), "transcript text")
    if normalized:
        return {"turns": normalized, "text": None}
    return {"turns": [], "text": fallback} if fallback else None


def normalize_meeting(raw: Any, selection: Selection) -> dict:
    if not isinstance(raw, dict):
        raise IncompleteSyncError("Read.ai returned an invalid meeting object")
    identity = meeting_id(raw.get("id"))
    start = _time(raw.get("start_time_ms"), "meeting start timestamp")
    end = _time(raw.get("end_time_ms"), "meeting end timestamp", optional=True)
    if end is not None and end < start:
        raise IncompleteSyncError("Read.ai returned inconsistent meeting timestamps")
    participants = raw.get("participants")
    if participants is None:
        participants = []
    if not isinstance(participants, list) or any(not isinstance(p, dict) for p in participants):
        raise IncompleteSyncError("Read.ai returned invalid participants")
    names = sorted(
        {name for p in participants if (name := _text(p.get("name"), "participant name"))}
    )
    sections = {}
    availability = {}
    for section in ("summary", "transcript", "metrics"):
        selected = getattr(selection, f"include_{section}")
        value = None
        if selected and end is not None:
            if section == "summary":
                value = _text(raw.get(section), section) or None
            elif section == "transcript":
                value = _transcript(raw.get(section), start)
            else:
                incoming = raw.get(section)
                if incoming is not None and not isinstance(incoming, dict):
                    raise IncompleteSyncError("Read.ai returned an invalid metrics object")
                value = _metrics(incoming) or None
        sections[section] = value
        availability[section] = (
            "unselected" if not selected else "available" if value is not None else "unavailable"
        )
    return {
        "id": identity,
        "title": _text(raw.get("title"), "meeting title") or f"Read.ai meeting {identity}",
        "url": f"https://app.read.ai/analytics/meetings/{identity}",
        "start_time_ms": start,
        "end_time_ms": end,
        "platform": _text(raw.get("platform"), "meeting platform"),
        "participants": names,
        "active": end is None,
        "sections": sections,
        "availability": availability,
    }


def _utc(stamp: int) -> str:
    return datetime.fromtimestamp(stamp / 1000, UTC).isoformat().replace("+00:00", "Z")


def _metric_lines(value: dict, prefix: str = "") -> list[str]:
    lines = []
    for name, metric in sorted(value.items()):
        label = f"{prefix}.{name}" if prefix else name
        if isinstance(metric, dict):
            lines.extend(_metric_lines(metric, label))
        else:
            lines.append(f"- {label}: {metric}")
    return lines


def render_meeting(meeting: dict) -> dict | None:
    sections = meeting["sections"]
    if all(value is None for value in sections.values()):
        return None
    parts = []
    if sections["summary"] is not None:
        parts.extend(["## Summary", sections["summary"], ""])
    parts.extend(
        [
            "## Meeting",
            f"Title: {meeting['title']}",
            f"Meeting ID: {meeting['id']}",
            f"Started: {_utc(meeting['start_time_ms'])}",
        ]
    )
    if meeting["end_time_ms"] is not None:
        parts.append(f"Ended: {_utc(meeting['end_time_ms'])}")
    if meeting["platform"]:
        parts.append(f"Platform: {meeting['platform']}")
    if meeting["participants"]:
        parts.append(f"Participants: {', '.join(meeting['participants'])}")
    parts.extend([f"Report: {meeting['url']}", ""])
    if sections["metrics"] is not None:
        parts.extend(["## Metrics (provider values)", *_metric_lines(sections["metrics"]), ""])
    transcript = sections["transcript"]
    if transcript is not None:
        parts.append("## Transcript")
        for turn in transcript["turns"]:
            millis = turn["offset_ms"]
            seconds, fraction = divmod(millis, 1000)
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            parts.append(
                f"[{hours:02}:{minutes:02}:{seconds:02}.{fraction:03}] "
                f"{turn['speaker']}: {turn['text']}"
            )
        if not transcript["turns"]:
            parts.append(transcript["text"])
    return {
        "id": meeting["id"],
        "title": meeting["title"],
        "content": "\n".join(parts).strip() + "\n",
        "url": meeting["url"],
        "_deleted": False,
    }
