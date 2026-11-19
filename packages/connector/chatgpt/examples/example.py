"""Two-snapshot demo: sync a synthetic ChatGPT export into cognee, ask it a
question, then re-sync with one conversation removed to show forget-on-delete.

No real ChatGPT export is needed — this builds one. Needs an LLM configured
(see the repo root .env / README); run with:

    uv run python examples/example.py
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import cognee

from cognee_community_connector_chatgpt import chatgpt_source

DATASET = "chatgpt-example"


def _conversation(conv_id, title, turns):
    mapping = {"root": {"id": "root", "parent": None, "children": [], "message": None}}
    parent = "root"
    for i, (role, text) in enumerate(turns):
        node_id = f"n{i}"
        mapping[node_id] = {
            "id": node_id,
            "parent": parent,
            "children": [],
            "message": {
                "id": node_id,
                "author": {"role": role},
                "recipient": "all",
                "content": {"content_type": "text", "parts": [text]},
                "metadata": {},
            },
        }
        mapping[parent]["children"].append(node_id)
        parent = node_id
    return {
        "id": conv_id,
        "conversation_id": conv_id,
        "title": title,
        "current_node": parent,
        "mapping": mapping,
    }


async def main():
    export_dir = Path(tempfile.mkdtemp()) / "export"
    export_dir.mkdir(parents=True)

    conversations = [
        _conversation(
            "c-db",
            "Choosing a vector database",
            [
                ("user", "What vector database should I use for a small RAG app?"),
                (
                    "assistant",
                    "For a small app, start with LanceDB: it is embedded, needs no server, "
                    "and scales to a few million vectors before you'd need anything else.",
                ),
            ],
        ),
        _conversation(
            "c-migration",
            "Planning a database migration",
            [
                ("user", "We're migrating from MySQL to Postgres, what should I watch for?"),
                (
                    "assistant",
                    "Check enum handling, case-sensitive identifiers, and any MySQL-specific "
                    "functions in stored procedures - those three cause the most migration bugs.",
                ),
            ],
        ),
    ]
    (export_dir / "conversations.json").write_text(json.dumps(conversations), encoding="utf-8")

    print("--- First sync: two conversations ---")
    await cognee.remember(
        chatgpt_source(str(export_dir)),
        dataset_name=DATASET,
    )

    # Fresh session per question: recall() otherwise shares 'default_session' and
    # feeds its last Q&A turns into the prompt, so earlier runs' answers can leak in.
    answer = await cognee.recall(
        "What vector database did I decide to use?",
        datasets=[DATASET],
        session_id=f"chatgpt-example-{uuid4().hex}",
    )
    for item in answer:
        print(item.text if hasattr(item, "text") else item)

    print("\n--- Second sync: the database-migration chat was deleted upstream ---")
    (export_dir / "conversations.json").write_text(json.dumps([conversations[0]]), encoding="utf-8")
    await cognee.remember(
        chatgpt_source(str(export_dir)),
        dataset_name=DATASET,
    )

    # Check the retrieved context, not an LLM answer: an LLM can still answer a
    # MySQL question from general knowledge even when the chat is gone.
    context = await cognee.recall(
        "What should I watch for in a MySQL to Postgres migration?",
        datasets=[DATASET],
        query_type=cognee.SearchType.CHUNKS,
        top_k=5,
    )
    still_there = any("Planning a database migration" in (item.text or "") for item in context)
    print(f"Migration chat still in retrieved context: {still_there}")
    print("PASS: deleted conversation was forgotten" if not still_there else "FAIL")

    shutil.rmtree(export_dir.parent, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
