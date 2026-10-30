"""IMAP mailbox data-source connector for cognee.

Connects to standard IMAP servers and yields email messages
as structured cognee memory documents.
"""

import email
import hashlib
import imaplib
import os
import re
from collections.abc import Iterator
from email.header import decode_header
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("imap_connector")

IMAP_TABLE_NAME = "imap_emails"
IMAP_SOURCE_NAME = "imap"

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _decode_mime_header(header_value: str | None) -> str:
    if not header_value:
        return ""
    parts = decode_header(header_value)
    decoded_chunks: list[str] = []
    for part, enc in parts:
        if isinstance(part, bytes):
            try:
                decoded_chunks.append(part.decode(enc or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded_chunks.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_chunks.append(str(part))
    return " ".join("".join(decoded_chunks).split())


def _extract_email_body(msg: email.message.Message) -> str:
    """Extract plain text or converted HTML body from an email.message object."""
    plain_chunks: list[str] = []
    html_chunks: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition", ""))
            if "attachment" in cdisp:
                continue

            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    text = payload.decode("utf-8", errors="replace")

                if ctype == "text/plain":
                    plain_chunks.append(text)
                elif ctype == "text/html":
                    html_chunks.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")

            if msg.get_content_type() == "text/html":
                html_chunks.append(text)
            else:
                plain_chunks.append(text)

    if plain_chunks:
        return "\n".join(plain_chunks).strip()
    if html_chunks:
        cleaned = _HTML_TAG_RE.sub(" ", "\n".join(html_chunks))
        return " ".join(cleaned.split()).strip()
    return ""


def parse_raw_email(raw_bytes: bytes, uid: str = "") -> dict[str, Any]:
    """Parse raw RFC 822 email bytes into a document row."""
    msg = email.message_from_bytes(raw_bytes)

    subject = _decode_mime_header(msg.get("Subject")) or "(No Subject)"
    from_addr = _decode_mime_header(msg.get("From")) or "Unknown Sender"
    to_addr = _decode_mime_header(msg.get("To")) or ""
    date_str = msg.get("Date") or ""
    message_id = msg.get("Message-ID") or uid or hashlib.sha256(raw_bytes).hexdigest()[:16]

    body = _extract_email_body(msg)

    doc_id = f"imap:{message_id.strip('<>')}"
    content = (
        f"# Subject: {subject}\n\n"
        f"**From:** {from_addr}\n"
        f"**To:** {to_addr}\n"
        f"**Date:** {date_str}\n\n"
        f"### Message Body\n{body}"
    )

    return {
        "id": doc_id,
        "message_id": message_id,
        "subject": subject,
        "from": from_addr,
        "to": to_addr,
        "date": date_str,
        "content": content,
    }


def iter_imap_messages(
    host: str | None = None,
    username: str | None = None,
    password: str | None = None,
    port: int = 993,
    folder: str = "INBOX",
    limit: int = 50,
    client: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed email documents from an IMAP mailbox."""
    if client is not None:
        imap_client = client
    else:
        resolved_host = host or os.environ.get("IMAP_HOST")
        resolved_user = username or os.environ.get("IMAP_USERNAME")
        resolved_pass = password or os.environ.get("IMAP_PASSWORD")

        if not resolved_host or not resolved_user or not resolved_pass:
            raise ValueError("IMAP connection details required: host, username, and password.")

        imap_client = imaplib.IMAP4_SSL(resolved_host, port=port)
        imap_client.login(resolved_user, resolved_pass)

    try:
        imap_client.select(folder)
        status, data = imap_client.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return

        msg_nums = data[0].split()
        # Take latest messages up to limit
        target_nums = msg_nums[-limit:] if len(msg_nums) > limit else msg_nums

        for num in reversed(target_nums):
            fetch_status, fetch_data = imap_client.fetch(num, "(RFC822)")
            if fetch_status != "OK" or not fetch_data:
                continue

            for part in fetch_data:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw_bytes = part[1]
                    yield parse_raw_email(raw_bytes, uid=str(num.decode() if isinstance(num, bytes) else num))
                    break
    finally:
        if client is None:
            try:
                imap_client.close()
                imap_client.logout()
            except Exception:
                pass


def imap_source(
    host: str | None = None,
    username: str | None = None,
    password: str | None = None,
    port: int = 993,
    folder: str = "INBOX",
    limit: int = 50,
    client: Any = None,
):
    """Create a dlt source yielding IMAP emails as documents."""
    import dlt

    @dlt.resource(name=IMAP_TABLE_NAME, primary_key="id", write_disposition="replace")
    def imap_emails():
        count = 0
        for email_item in iter_imap_messages(
            host=host,
            username=username,
            password=password,
            port=port,
            folder=folder,
            limit=limit,
            client=client,
        ):
            count += 1
            yield email_item
        logger.info("IMAP: synced %d email(s).", count)

    @dlt.source(name=IMAP_SOURCE_NAME)
    def _imap():
        return imap_emails

    source = _imap()
    setattr(source, DOCUMENT_SOURCE_ATTR, IMAP_SOURCE_NAME)
    return source
