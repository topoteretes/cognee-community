"""Example of ingesting Screenpipe local context into cognee."""

import asyncio
from unittest.mock import MagicMock
from cognee_community_connector_screenpipe import (
    iter_screenpipe_meetings,
    iter_screenpipe_memories,
    iter_screenpipe_screen_text,
    screenpipe_source,
)


def mock_screenpipe_client(endpoint, **kwargs):
    """Simulate Screenpipe local REST API endpoints for offline demo."""
    if endpoint == "/meetings":
        return [
            {
                "id": "meet_001",
                "title": "Weekly Engineering Sync",
                "app_name": "Zoom",
                "meeting_start": "2026-11-02T10:00:00Z",
                "meeting_end": "2026-11-02T10:45:00Z",
                "attendees": ["Alice", "Bob", "Charlie"],
                "user_notes": "Reviewed Q4 milestones and cloud migration.",
            }
        ]
    elif endpoint == "/meetings/meet_001/transcript":
        return [
            {
                "speaker": "Alice",
                "timestamp": "10:05:00",
                "text": "Let's migrate our vector store to Postgres pgvector next week.",
            },
            {
                "speaker": "Bob",
                "timestamp": "10:06:15",
                "text": "Agreed, I will open the migration pull request tomorrow.",
            },
        ]
    elif endpoint == "/memories":
        return [
            {
                "id": "mem_001",
                "content": "Remember to renew AWS credentials before Nov 15.",
                "tags": ["devops", "aws"],
                "created_at": "2026-11-02T11:00:00Z",
                "updated_at": "2026-11-02T11:00:00Z",
            }
        ]
    elif endpoint == "/search":
        return [
            {
                "app_name": "VS Code",
                "text": "def test_migration_pipeline():\n    assert run_pipeline() is True",
                "timestamp": "2026-11-02T14:15:00Z",
            }
        ]
    return []


def main():
    print("Testing Screenpipe data extractors with mock local instance...")

    # Test meetings extraction
    meetings = list(iter_screenpipe_meetings(client=mock_screenpipe_client))
    print(f"Extracted {len(meetings)} meeting(s):")
    for m in meetings:
        print(f" - [{m['id']}] {m['title']} ({m['meeting_start']} - {m['meeting_end']})")
        print(f"   Content snippet:\n   {m['content'][:120]}...\n")

    # Test memories extraction
    memories = list(iter_screenpipe_memories(client=mock_screenpipe_client))
    print(f"Extracted {len(memories)} memory(ies):")
    for mem in memories:
        print(f" - [{mem['id']}] {mem['title']}: {mem['content']}")

    # Test screen OCR extraction
    screen_docs = list(
        iter_screenpipe_screen_text(
            app_allowlist=["VS Code"],
            client=mock_screenpipe_client,
        )
    )
    print(f"\nExtracted {len(screen_docs)} screen activity document(s):")
    for doc in screen_docs:
        print(f" - [{doc['id']}] {doc['title']}")


if __name__ == "__main__":
    main()
