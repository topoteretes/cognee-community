"""Verify the stale-export guard against the real cognee pipeline: an export
missing a conversation that was more recently updated than anything left in
it must refuse to sync, leaving existing memory untouched -- not silently
forget that conversation the way a plain replace snapshot would."""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import cognee

from cognee_community_connector_chatgpt import StaleExportError, chatgpt_source

DATASET = "chatgpt-verify-stale"


def _conversation(conv_id, title, turns, update_time=None):
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
    conv = {
        "id": conv_id,
        "conversation_id": conv_id,
        "title": title,
        "current_node": parent,
        "mapping": mapping,
    }
    if update_time is not None:
        conv["update_time"] = update_time
    return conv


async def main():
    export_dir = Path(tempfile.mkdtemp()) / "export"
    export_dir.mkdir(parents=True)

    recent = _conversation(
        "c-recent", "A recently touched chat", [("user", "hi")], update_time=500.0
    )
    quiet = _conversation("c-quiet", "An untouched chat", [("user", "yo")], update_time=100.0)
    (export_dir / "conversations.json").write_text(json.dumps([recent, quiet]), encoding="utf-8")

    print("--- First sync: both conversations present ---")
    await cognee.remember(chatgpt_source(str(export_dir)), dataset_name=DATASET)
    before = await cognee.recall("recently touched chat", datasets=[DATASET], only_context=True)
    found_before = any("recently touched" in (i.text or "") for i in before)
    print(f"'c-recent' content found before: {found_before}")

    print("\n--- Second sync: c-recent vanished, nothing left is as new as it was ---")
    (export_dir / "conversations.json").write_text(json.dumps([quiet]), encoding="utf-8")

    blocked = False
    try:
        await cognee.remember(chatgpt_source(str(export_dir)), dataset_name=DATASET)
    except Exception as exc:
        # dlt wraps the connector's own StaleExportError in its own
        # extraction/pipeline exception types; walk the cause chain.
        cause = exc
        while cause is not None and not isinstance(cause, StaleExportError):
            cause = cause.__cause__
        if cause is not None:
            blocked = True
            print(f"Sync correctly refused: {cause}")
        else:
            raise

    print(f"\nSync was blocked by the stale-export guard: {blocked}")

    print("\n--- Confirming nothing was lost: querying again after the refused sync ---")
    after = await cognee.recall("recently touched chat", datasets=[DATASET], only_context=True)
    still_there = any("recently touched" in (i.text or "") for i in after)
    print(f"'c-recent' content still present after the refused sync: {still_there}")

    print("\n=== VERDICT ===")
    if blocked and still_there:
        print("PASS: the stale-export guard refused the sync and left existing memory untouched.")
    else:
        print(f"FAIL or inconclusive: blocked={blocked} still_there={still_there}")

    shutil.rmtree(export_dir.parent, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
