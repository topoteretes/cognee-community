# Subtitles Data-Source Connector for Cognee (`cognee-community-connector-subtitles`)

Ingest spoken transcripts from meeting recordings (Zoom, Google Meet, Microsoft Teams), screen recordings (OBS), and audio transcribers (OpenAI Whisper) directly into Cognee's knowledge graph.

## Why use this connector?
When raw `.srt` or `.vtt` files are ingested into memory directly, knowledge graphs are corrupted by timing noise (`00:01:23,456 --> 00:01:25,900`), cue numbers, and mid-sentence splits.

This connector:
- **Cleans Transcripts**: Automatically strips timing headers, cue indices, WebVTT `NOTE` and `STYLE` blocks, and HTML formatting tags (`<c>`, `<b>`, `<i>`).
- **Extracts Speakers**: Parses WebVTT `<v Speaker>` voice tags and `Speaker:` prefixes into first-class metadata.
- **Dialogue Paragraph Merging**: Merges short, fragmented 2-3 second speech cues into readable, context-rich dialogue paragraphs.
- **Timestamp Indexing**: Preserves standard start timestamps (`[HH:MM:SS] Speaker: text`) so graph search results link back to the exact recording second.
- **Snapshot Replace & Forget-On-Delete**: Deletions in your local folder propagate cleanly; removed files are automatically purged via Cognee's `orphan_cleanup`.

---

## Installation

```bash
pip install cognee-community-connector-subtitles
```

---

## Quickstart

```python
import cognee
from cognee_community_connector_subtitles import subtitles_source

async def main():
    # Ingest a directory of .srt and .vtt files
    source = subtitles_source("/path/to/transcripts", recursive=True)
    
    # Ingest into cognee memory
    await cognee.add(source)
    await cognee.cognify()
    
    # Query your meeting notes with precise moment retrieval
    results = await cognee.search("What feedback did Carol have on the checkout conversion flow?")
    print(results)
```
