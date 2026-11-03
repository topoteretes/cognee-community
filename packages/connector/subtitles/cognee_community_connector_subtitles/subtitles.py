"""DLT source for subtitle files (SRT / WebVTT) with paragraph merging & snapshot replace.

Ingests spoken transcripts from Zoom, Microsoft Teams, Google Meet, OBS, and
OpenAI Whisper into cognee memory as clean, timestamped markdown documents.

Unlike raw text ingestion (which pollutes knowledge graphs with cue indices,
millisecond timestamps, and mid-sentence splits), this connector:
1. Strips timestamp lines, cue numbers, and WebVTT styling/NOTE blocks.
2. Extracts `<v Speaker>` voice tags and `Speaker:` prefixes.
3. Merges short 2-3 second speech fragments into readable dialogue paragraphs.
4. Preserves clickable start timestamps: `[HH:MM:SS] Speaker: text`.
5. Uses full snapshot with ``write_disposition="replace"`` and content-hash `data_id`
   so deleted files are forgotten via cognee's `orphan_cleanup`, and unchanged
   files are not re-cognified.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

SUBTITLES_SOURCE_NAME = "subtitles"
SUBTITLES_TABLE_NAME = "subtitles_documents"

_EXTRA_HINT = (
    'The subtitles connector requires cognee and dlt: pip install "cognee-community-connector-subtitles"'
)

# Regular expressions for cleaning HTML & WebVTT tags
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_VOICE_TAG_RE = re.compile(r"<v(?:[.\w\s-]+)?\s+([^>]+)>(.*?)(?:</v>|$)", re.DOTALL | re.IGNORECASE)
_SPEAKER_PREFIX_RE = re.compile(r"^([A-Za-z0-9_\s.-]{2,30}):\s*(.*)$")

# Timestamp matchers
# SRT: 00:01:23,456 --> 00:01:28,910
_SRT_TIMING_RE = re.compile(
    r"^(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})"
)
# WebVTT: 00:01:23.456 --> 00:01:28.910 or 01:23.456 --> 01:28.910 (plus optional settings)
_VTT_TIMING_RE = re.compile(
    r"^((?:\d{1,2}:)?\d{2}:\d{2}\.\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}\.\d{3})(?:.*)?$"
)


@dataclass
class SubtitleCue:
    """A single timestamped speech fragment from an SRT or WebVTT file."""

    start_seconds: float
    end_seconds: float
    start_str: str
    end_str: str
    speaker: str | None
    text: str


@dataclass
class SubtitleDocument:
    """A fully ingested subtitle file rendered as clean dialogue markdown."""

    file_path: str
    title: str
    file_format: str
    markdown_content: str
    content_hash: str
    duration_seconds: float
    speakers: list[str]
    cue_count: int


def _timestamp_to_seconds(ts_str: str) -> float:
    """Converts 'HH:MM:SS,mmm' or 'MM:SS.mmm' into float seconds."""
    ts = ts_str.replace(",", ".").strip()
    parts = ts.split(":")
    if len(parts) == 3:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
        return hours * 3600.0 + minutes * 60.0 + seconds
    elif len(parts) == 2:
        minutes = float(parts[0])
        seconds = float(parts[1])
        return minutes * 60.0 + seconds
    return 0.0


def _format_seconds(seconds: float) -> str:
    """Formats float seconds into standard [HH:MM:SS]."""
    total_sec = int(round(seconds))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def _clean_text(raw_text: str) -> tuple[str | None, str]:
    """Strips formatting tags and extracts speaker name if present."""
    text = raw_text.strip()
    speaker: str | None = None

    # Check for WebVTT <v Speaker Name> tag
    voice_match = _VOICE_TAG_RE.search(text)
    if voice_match:
        speaker = voice_match.group(1).strip()
        text = _VOICE_TAG_RE.sub(r"\2", text)

    # Strip any remaining tags (<b>, <i>, <c>, timestamps)
    text = _HTML_TAG_RE.sub("", text).strip()

    # If no speaker from voice tag, check for 'Speaker: text' prefix
    if not speaker:
        prefix_match = _SPEAKER_PREFIX_RE.match(text)
        if prefix_match:
            speaker = prefix_match.group(1).strip()
            text = prefix_match.group(2).strip()

    return speaker, text


def parse_srt(file_content: str) -> list[SubtitleCue]:
    """Parses standard SubRip (.srt) subtitle content into structured cues."""
    # Normalize line endings and strip BOM
    content = file_content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())
    cues: list[SubtitleCue] = []

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        timing_line_idx = -1
        timing_match = None

        for idx, line in enumerate(lines[:3]):
            timing_match = _SRT_TIMING_RE.match(line)
            if timing_match:
                timing_line_idx = idx
                break

        if not timing_match:
            continue

        start_str = timing_match.group(1)
        end_str = timing_match.group(2)
        start_sec = _timestamp_to_seconds(start_str)
        end_sec = _timestamp_to_seconds(end_str)

        text_lines = lines[timing_line_idx + 1 :]
        combined_text = " ".join(text_lines)
        speaker, clean_text = _clean_text(combined_text)

        if clean_text:
            cues.append(
                SubtitleCue(
                    start_seconds=start_sec,
                    end_seconds=end_sec,
                    start_str=start_str,
                    end_str=end_str,
                    speaker=speaker,
                    text=clean_text,
                )
            )

    return cues


def parse_vtt(file_content: str) -> list[SubtitleCue]:
    """Parses WebVTT (.vtt) subtitle content into structured cues."""
    content = file_content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())
    cues: list[SubtitleCue] = []

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        # Skip header, NOTE, or STYLE blocks
        first_line = lines[0].upper()
        if first_line.startswith("WEBVTT") or first_line.startswith("NOTE") or first_line.startswith("STYLE"):
            # Check if this block also contains a cue after header
            if len(lines) == 1:
                continue
            lines = lines[1:]

        timing_line_idx = -1
        timing_match = None

        for idx, line in enumerate(lines[:3]):
            timing_match = _VTT_TIMING_RE.match(line)
            if timing_match:
                timing_line_idx = idx
                break

        if not timing_match:
            continue

        start_str = timing_match.group(1)
        end_str = timing_match.group(2)
        start_sec = _timestamp_to_seconds(start_str)
        end_sec = _timestamp_to_seconds(end_str)

        text_lines = lines[timing_line_idx + 1 :]
        combined_text = " ".join(text_lines)
        speaker, clean_text = _clean_text(combined_text)

        if clean_text:
            cues.append(
                SubtitleCue(
                    start_seconds=start_sec,
                    end_seconds=end_sec,
                    start_str=start_str,
                    end_str=end_str,
                    speaker=speaker,
                    text=clean_text,
                )
            )

    return cues


def merge_cues_into_dialogue(
    cues: list[SubtitleCue],
    max_gap_seconds: float = 3.5,
) -> tuple[str, list[str], float]:
    """Merges sequential short cues into readable dialogue paragraphs.

    Groups consecutive cues when spoken by the same speaker (or continuous speech)
    separated by less than ``max_gap_seconds``.
    """
    if not cues:
        return "", [], 0.0

    paragraphs: list[str] = []
    current_speaker = cues[0].speaker
    current_start = cues[0].start_seconds
    current_end = cues[0].end_seconds
    current_texts = [cues[0].text]
    all_speakers: set[str] = set()
    if current_speaker:
        all_speakers.add(current_speaker)

    for next_cue in cues[1:]:
        if next_cue.speaker:
            all_speakers.add(next_cue.speaker)

        gap = next_cue.start_seconds - current_end
        same_speaker = next_cue.speaker == current_speaker or (
            current_speaker is None and next_cue.speaker is None
        )

        if same_speaker and gap <= max_gap_seconds:
            # Append to current paragraph
            current_texts.append(next_cue.text)
            current_end = max(current_end, next_cue.end_seconds)
        else:
            # Flush current paragraph
            ts = _format_seconds(current_start)
            body = " ".join(current_texts)
            if current_speaker:
                paragraphs.append(f"[{ts}] **{current_speaker}**: {body}")
            else:
                paragraphs.append(f"[{ts}] {body}")

            # Start new paragraph
            current_speaker = next_cue.speaker
            current_start = next_cue.start_seconds
            current_end = next_cue.end_seconds
            current_texts = [next_cue.text]

    # Flush final paragraph
    if current_texts:
        ts = _format_seconds(current_start)
        body = " ".join(current_texts)
        if current_speaker:
            paragraphs.append(f"[{ts}] **{current_speaker}**: {body}")
        else:
            paragraphs.append(f"[{ts}] {body}")

    total_duration = cues[-1].end_seconds if cues else 0.0
    return "\n\n".join(paragraphs), sorted(all_speakers), total_duration


def process_subtitle_file(file_path: Path, max_gap_seconds: float = 3.5) -> SubtitleDocument | None:
    """Reads and transforms a single .srt or .vtt file into a markdown document."""
    ext = file_path.suffix.lower()
    if ext not in (".srt", ".vtt"):
        return None

    try:
        raw_text = file_path.read_text(encoding="utf-8-sig")
    except Exception:
        # Fallback to latin-1 if file encoding has non-standard characters
        raw_text = file_path.read_text(encoding="latin-1")

    if ext == ".srt":
        cues = parse_srt(raw_text)
    else:
        cues = parse_vtt(raw_text)

    if not cues:
        return None

    dialogue, speakers, duration = merge_cues_into_dialogue(cues, max_gap_seconds=max_gap_seconds)
    title = file_path.stem.replace("_", " ").replace("-", " ").title()

    markdown_doc = f"# Transcript: {title}\n\n"
    markdown_doc += f"- **Source File:** `{file_path.name}`\n"
    markdown_doc += f"- **Format:** {ext.upper().lstrip('.')}\n"
    markdown_doc += f"- **Duration:** {_format_seconds(duration)}\n"
    if speakers:
        markdown_doc += f"- **Speakers:** {', '.join(speakers)}\n"
    markdown_doc += f"\n---\n\n{dialogue}\n"

    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    return SubtitleDocument(
        file_path=str(file_path.resolve()),
        title=title,
        file_format=ext.lstrip("."),
        markdown_content=markdown_doc,
        content_hash=content_hash,
        duration_seconds=duration,
        speakers=speakers,
        cue_count=len(cues),
    )


def subtitles_source(
    path: str | Path,
    recursive: bool = True,
    max_gap_seconds: float = 3.5,
) -> Any:
    """Create a dlt source that ingests subtitle transcripts (.srt / .vtt) into cognee.

    Args:
        path: Path to a directory containing subtitle files or a single subtitle file.
        recursive: Whether to search subdirectories when path is a directory.
        max_gap_seconds: Maximum pause between same-speaker cues to merge into a single paragraph.

    Returns:
        A dlt source suitable for ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    target_path = Path(path).resolve()
    if not target_path.exists():
        raise FileNotFoundError(f"Subtitle path does not exist: {target_path}")

    @dlt.resource(
        name=SUBTITLES_TABLE_NAME,
        write_disposition="replace",
        primary_key="id",
    )
    def subtitles_documents() -> Iterable[dict[str, Any]]:
        if target_path.is_file():
            files = [target_path]
        else:
            pattern = "**/*" if recursive else "*"
            files = [p for p in target_path.glob(pattern) if p.suffix.lower() in (".srt", ".vtt")]

        for file_item in sorted(files):
            doc = process_subtitle_file(file_item, max_gap_seconds=max_gap_seconds)
            if doc:
                yield {
                    "id": f"subtitles:{doc.content_hash[:16]}:{Path(doc.file_path).name}",
                    "name": doc.title,
                    "text": doc.markdown_content,
                    "format": doc.file_format,
                    "duration_seconds": doc.duration_seconds,
                    "speakers": doc.speakers,
                    "cue_count": doc.cue_count,
                    "file_path": doc.file_path,
                }

    @dlt.source(name=SUBTITLES_SOURCE_NAME)
    def source():
        return subtitles_documents

    src = source()
    setattr(src, DOCUMENT_SOURCE_ATTR, "subtitles")
    return src
