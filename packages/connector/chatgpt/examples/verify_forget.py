"""Verify forget-on-delete definitively: inspect the actual retrieval context
cognee builds for the post-deletion query, instead of trusting the LLM's
answer (which can draw on its own training data even with empty context)."""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import cognee

from cognee_community_connector_chatgpt import chatgpt_source

DATASET = "chatgpt-verify-forget"


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

    kept = _conversation(
        "c-db",
        "Choosing a vector database",
        [
            ("user", "What vector database should I use?"),
            ("assistant", "LanceDB is a great embedded option."),
        ],
    )
    removed = _conversation(
        "c-migration",
        "Planning a database migration",
        [
            ("user", "We're migrating from MySQL to Postgres, specific gotcha?"),
            (
                "assistant",
                "Watch for the ZORBLAXIAN_ENUM_QUIRK-7f3a sentinel value in legacy enum columns.",
            ),
        ],
    )

    (export_dir / "conversations.json").write_text(json.dumps([kept, removed]), encoding="utf-8")
    await cognee.remember(chatgpt_source(str(export_dir)), dataset_name=DATASET)

    print("=== BEFORE deletion: context for the migration question ===")
    items = await cognee.recall(
        "What is the ZORBLAXIAN_ENUM_QUIRK sentinel value about?",
        datasets=[DATASET],
        only_context=True,
    )
    for item in items:
        print(repr(item.text)[:500])
    found_before = any("ZORBLAXIAN" in (item.text or "") for item in items)
    print(f"-> sentinel string found in context BEFORE delete: {found_before}")

    (export_dir / "conversations.json").write_text(json.dumps([kept]), encoding="utf-8")
    await cognee.remember(chatgpt_source(str(export_dir)), dataset_name=DATASET)

    print("\n=== AFTER deletion: context for the same question ===")
    items = await cognee.recall(
        "What is the ZORBLAXIAN_ENUM_QUIRK sentinel value about?",
        datasets=[DATASET],
        only_context=True,
    )
    for item in items:
        print(repr(item.text)[:500])
    found_after = any("ZORBLAXIAN" in (item.text or "") for item in items)
    print(f"-> sentinel string found in context AFTER delete: {found_after}")

    print("\n=== VERDICT ===")
    if found_before and not found_after:
        print("PASS: forget-on-delete actually removed the content from retrieval context.")
    else:
        print(f"FAIL or inconclusive: before={found_before} after={found_after}")

    shutil.rmtree(export_dir.parent, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
