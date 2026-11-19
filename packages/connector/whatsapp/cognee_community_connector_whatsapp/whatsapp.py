"""WhatsApp chat export connector for cognee.

Parses official WhatsApp chat exports (.txt or .zip containing _chat.txt)
into structured messages for cognee.
"""

import hashlib
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("whatsapp_connector")

WHATSAPP_TABLE_NAME = "whatsapp_messages"
WHATSAPP_SOURCE_NAME = "whatsapp"

# Match standard WhatsApp export line prefixes:
# Android / International:
# 25/12/2023, 10:30 - John Doe: Hello
# 12/25/23, 10:30 AM - John Doe: Hello
# iPhone / iOS:
# [25/12/2023, 10:30:15] John Doe: Hello
# [12/25/23, 10:30:15 AM] John Doe: Hello
_MSG_REGEXES = [
    # [timestamp] Sender: text
    re.compile(r"^\[(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}[,\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)\]\s+([^:]+?):\s+(.*)$"),
    # timestamp - Sender: text
    re.compile(r"^(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}[,\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)\s*-\s+([^:]+?):\s+(.*)$"),
]

# System notices in export
_SYSTEM_PATTERNS = [
    "messages and calls are end-to-end encrypted",
    "created group",
    "added you",
    "left the group",
    "security code changed",
    "changed the group description",
    "changed this group's icon",
    "you were added",
    "this message was deleted",
]


def _is_system_message(sender: str, text: str) -> bool:
    lower_text = text.lower().strip()
    lower_sender = sender.lower().strip()
    if any(pat in lower_text for pat in _SYSTEM_PATTERNS):
        return True
    return bool(any(pat in lower_sender for pat in _SYSTEM_PATTERNS))


def _read_export_lines(file_path: str | Path) -> tuple[str, list[str]]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"WhatsApp export file not found: {file_path}")

    chat_name = path.stem.replace("WhatsApp Chat - ", "").replace("_chat", "")

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path, "r") as z:
            txt_files = [f for f in z.namelist() if f.endswith(".txt")]
            if not txt_files:
                raise ValueError(f"No .txt chat file found inside zip archive: {file_path}")
            # Usually '_chat.txt'
            target_name = next((f for f in txt_files if "_chat.txt" in f), txt_files[0])
            content = z.read(target_name).decode("utf-8", errors="replace")
            lines = content.splitlines()
    else:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

    return chat_name, [line.rstrip("\r\n") for line in lines]


def iter_whatsapp_messages(file_path: str | Path, chat_name: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield parsed messages from a WhatsApp chat export file."""
    detected_name, lines = _read_export_lines(file_path)
    resolved_chat_name = chat_name or detected_name or "WhatsApp Chat"

    current_msg = None

    for line in lines:
        if not line.strip():
            if current_msg:
                current_msg["text"] += "\n"
            continue

        matched = False
        for regex in _MSG_REGEXES:
            m = regex.match(line)
            if m:
                matched = True
                if current_msg and not _is_system_message(
                    current_msg["sender"], current_msg["text"]
                ):
                    yield _finalize_message(current_msg)
                raw_time, sender, text = m.groups()
                current_msg = {
                    "chat_name": resolved_chat_name,
                    "timestamp": raw_time.strip(),
                    "sender": sender.strip(),
                    "text": text.strip(),
                }
                break

        if not matched and current_msg:
            # Continuation of multi-line message
            current_msg["text"] += "\n" + line

    if current_msg and not _is_system_message(current_msg["sender"], current_msg["text"]):
        yield _finalize_message(current_msg)


def _finalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    text = msg["text"].strip()
    chat_name = msg["chat_name"]
    sender = msg["sender"]
    ts = msg["timestamp"]

    # Generate stable ID from chat, sender, timestamp and hash of text
    content_hash = hashlib.sha256(f"{sender}:{ts}:{text}".encode()).hexdigest()[:16]
    msg_id = f"wa:{chat_name}:{ts}:{content_hash}"
    rendered_content = f"[{chat_name}] {ts} - {sender}: {text}"

    return {
        "id": msg_id,
        "chat_name": chat_name,
        "sender": sender,
        "timestamp": ts,
        "text": text,
        "title": f"WhatsApp Message from {sender} in {chat_name}",
        "content": rendered_content,
    }


def whatsapp_source(file_path: str | Path, chat_name: str | None = None):
    """Create a dlt source that yields WhatsApp chat export messages as documents."""
    import dlt

    @dlt.resource(name=WHATSAPP_TABLE_NAME, primary_key="id", write_disposition="replace")
    def whatsapp_messages():
        count = 0
        for msg in iter_whatsapp_messages(file_path, chat_name=chat_name):
            count += 1
            yield msg
        logger.info("WhatsApp: synced %d message(s).", count)

    @dlt.source(name=WHATSAPP_SOURCE_NAME)
    def _whatsapp():
        return whatsapp_messages

    source = _whatsapp()
    setattr(source, DOCUMENT_SOURCE_ATTR, WHATSAPP_SOURCE_NAME)
    return source
