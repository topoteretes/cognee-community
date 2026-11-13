"""Subtitle files (SRT / WebVTT) data-source connector for cognee."""

from .subtitles import (
    SUBTITLES_SOURCE_NAME,
    SUBTITLES_TABLE_NAME,
    SubtitleCue,
    SubtitleDocument,
    parse_srt,
    parse_vtt,
    subtitles_source,
)

__all__ = [
    "subtitles_source",
    "parse_srt",
    "parse_vtt",
    "SubtitleCue",
    "SubtitleDocument",
    "SUBTITLES_TABLE_NAME",
    "SUBTITLES_SOURCE_NAME",
]
