"""Fetch and format YouTube video captions/transcripts."""

from __future__ import annotations

from collections.abc import Sequence

from cognee.shared.logging_utils import get_logger

logger = get_logger("youtube_connector")


def fetch_video_captions(
    video_id: str,
    languages: Sequence[str] = ("en",),
    fallback_to_any: bool = True,
) -> str:
    """Fetch captions for a YouTube video using youtube-transcript-api.

    This uses youtube-transcript-api to retrieve publicly available manual or
    auto-generated transcripts without requiring OAuth permissions.

    Missing captions (e.g., transcripts disabled, not found, or private video)
    are treated as a normal outcome and return an empty string.

    Args:
        video_id: The 11-character YouTube video ID.
        languages: Preferred language codes (e.g., ``["en"]``).
        fallback_to_any: If True and preferred languages are unavailable,
            fall back to any available transcript on the video.

    Returns:
        The extracted captions formatted as plain text, or an empty string if
        no captions are available.
    """
    try:
        from youtube_transcript_api import (
            CouldNotRetrieveTranscript,
            NoTranscriptFound,
            TranscriptsDisabled,
            YouTubeTranscriptApi,
        )
    except ImportError as exc:
        logger.warning(
            "youtube-transcript-api is not installed; skipping captions for video %s: %s",
            video_id,
            exc,
        )
        return ""

    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        transcript = None
        # 1. Look for manually created or generated transcripts matching requested languages
        try:
            transcript = transcript_list.find_transcript(list(languages))
        except (NoTranscriptFound, Exception):
            if fallback_to_any:
                # 2. Pick the first available transcript (manual or auto-generated)
                for t in transcript_list:
                    transcript = t
                    break

        if transcript is None:
            return ""

        chunks = transcript.fetch()
        lines = [item["text"].strip() for item in chunks if item.get("text")]
        return " ".join(lines)

    except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript) as exc:
        logger.debug("No transcript available for video %s: %s", video_id, exc)
        return ""
    except Exception as exc:
        logger.warning("Error fetching captions for video %s: %s", video_id, exc)
        return ""
