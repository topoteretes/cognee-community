"""Generic IMAP connector for cognee, based on dlt's verified ``inbox`` source.

The dlt verified sources are distributed as editable source with ``dlt init``.
This module is an adapted/ejected version of that source's UID search, batched
resource, and MIME extraction flow.  It adds the mailbox semantics needed by a
long-lived cognee sync: UIDVALIDITY, per-folder state, deletion reconciliation,
CONDSTORE flag refreshes, and document-shaped rows.

The connector is read-only.  It only opens encrypted IMAP connections and
selects folders with ``readonly=True``.
"""

from __future__ import annotations

import email
import html
import imaplib
import re
import ssl
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, ClassVar, Literal
from urllib.parse import quote

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("imap_connector")

_UID_RE = re.compile(rb"\bUID\s+(\d+)\b", re.IGNORECASE)
_FLAGS_RE = re.compile(rb"\bFLAGS\s+\(([^)]*)\)", re.IGNORECASE)
_MODSEQ_RE = re.compile(rb"\bMODSEQ\s+\((\d+)\)", re.IGNORECASE)
_NUMBER_RE = re.compile(rb"\d+")


class _HTMLTextExtractor(HTMLParser):
    """Small, forgiving HTML-to-text converter for HTML-only mail."""

    _BLOCK_TAGS: ClassVar[set[str]] = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = (re.sub(r"[\t\r\f\v ]+", " ", line).strip() for line in self.parts_text.split("\n"))
        return "\n".join(line for line in lines if line)

    @property
    def parts_text(self) -> str:
        return "".join(self.parts)


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
        return html.unescape(parser.text()).strip()
    except Exception:
        # Malformed HTML must not make us drop the containing message.
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value)).strip()


def _decode_header(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    if isinstance(payload, str):
        return payload

    declared = part.get_content_charset()
    encodings = [declared, "utf-8", "windows-1252", "latin-1"]
    tried: set[str] = set()
    for encoding in encodings:
        if not encoding or encoding.lower() in tried:
            continue
        tried.add(encoding.lower())
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _body_and_attachments(message: Message) -> tuple[str, list[str]]:
    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = _decode_header(part.get_filename())
        if filename or disposition == "attachment":
            attachments.append(filename or "unnamed attachment")
            continue

        content_type = part.get_content_type().lower()
        if content_type == "text/plain":
            text = _decode_part(part).strip()
            if text:
                plain.append(text)
        elif content_type == "text/html":
            text = _html_to_text(_decode_part(part))
            if text:
                html_parts.append(text)

    return "\n\n".join(plain or html_parts), attachments


def _normalise_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.isoformat() if parsed else value
    except (TypeError, ValueError, OverflowError):
        return value


def _message_key(folder: str, uidvalidity: str, uid: int) -> str:
    """Encode the real composite key without relying on Message-ID."""
    return f"{quote(folder, safe='')}:{uidvalidity}:{uid}"


def parse_message(
    raw_message: bytes,
    *,
    folder: str,
    uidvalidity: str | int,
    uid: int,
    flags: Sequence[str] = (),
    modseq: int | None = None,
) -> dict[str, Any]:
    """Turn an RFC822 message into one readable, document-shaped dlt row.

    Parsing is deliberately best-effort: unknown charsets, malformed dates,
    and broken MIME parts degrade to replacement text instead of dropping mail.
    """
    try:
        message = email.message_from_bytes(raw_message, policy=policy.default)
    except Exception:
        message = email.message_from_bytes(raw_message, policy=policy.compat32)

    subject = _decode_header(message.get("Subject"))
    sender = _decode_header(message.get("From"))
    to = _decode_header(message.get("To"))
    cc = _decode_header(message.get("Cc"))
    date_value = _normalise_date(_decode_header(message.get("Date")))
    message_id = _decode_header(message.get("Message-ID"))
    in_reply_to = _decode_header(message.get("In-Reply-To"))
    references = _decode_header(message.get("References"))
    body, attachments = _body_and_attachments(message)
    validity = str(uidvalidity)
    row_id = _message_key(folder, validity, uid)

    # Include the envelope and thread headers in the document text.  Cognee's
    # document ingestion path uses title/content while retaining all other
    # columns in dlt as metadata.
    metadata_lines = [
        f"From: {sender}" if sender else "",
        f"To: {to}" if to else "",
        f"Cc: {cc}" if cc else "",
        f"Date: {date_value}" if date_value else "",
        f"Message-ID: {message_id}" if message_id else "",
        f"In-Reply-To: {in_reply_to}" if in_reply_to else "",
        f"References: {references}" if references else "",
        f"Attachments: {', '.join(attachments)}" if attachments else "",
    ]
    content = "\n".join(line for line in metadata_lines if line)
    if body:
        content = f"{content}\n\n{body}" if content else body

    return {
        "id": row_id,
        "folder": folder,
        "uidvalidity": validity,
        "uid": uid,
        "title": subject or "(no subject)",
        "content": content,
        "subject": subject,
        "from": sender,
        "to": to,
        "cc": cc,
        "date": date_value,
        "body": body,
        "attachment_names": ", ".join(attachments),
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "flags": " ".join(flags),
        "modseq": modseq,
        "url": f"imap://{quote(folder, safe='')}/{uid}",
        "_deleted": False,
    }


def _deleted_row(folder: str, uidvalidity: str | int, uid: int) -> dict[str, Any]:
    validity = str(uidvalidity)
    return {
        "id": _message_key(folder, validity, uid),
        "folder": folder,
        "uidvalidity": validity,
        "uid": uid,
        "_deleted": True,
    }


def _status_ok(status: Any) -> bool:
    if isinstance(status, bytes):
        status = status.decode(errors="replace")
    return str(status).upper() == "OK"


def _response_number(client: Any, name: str) -> int | None:
    try:
        response = client.response(name)
    except (AttributeError, KeyError):
        response = None
    values = response[1] if isinstance(response, tuple) and len(response) > 1 else response
    if not isinstance(values, (list, tuple)):
        values = [values]
    for value in values:
        if value is None:
            continue
        raw = value if isinstance(value, bytes) else str(value).encode()
        match = _NUMBER_RE.search(raw)
        if match:
            return int(match.group())
    # A small convenience for injected test clients.
    fallback = getattr(client, name.lower(), None)
    return int(fallback) if fallback is not None else None


def _capabilities(client: Any) -> set[str]:
    capabilities = getattr(client, "capabilities", ()) or ()
    return {
        (item.decode(errors="replace") if isinstance(item, bytes) else str(item)).upper()
        for item in capabilities
    }


def _select_folder(client: Any, folder: str) -> tuple[str, int | None, bool]:
    capabilities = _capabilities(client)
    has_condstore = "CONDSTORE" in capabilities
    if has_condstore and "ENABLE" in capabilities and hasattr(client, "enable"):
        # ENABLE CONDSTORE makes SELECT return HIGHESTMODSEQ without relying on
        # private imaplib SELECT-command internals.
        status, _ = client.enable("CONDSTORE")
        if not _status_ok(status):
            logger.warning("IMAP server advertised but did not enable CONDSTORE for %s", folder)
    # imaplib does not quote mailbox arguments. Keep simple atom-safe names
    # untouched (which also helps lightweight IMAP implementations), and quote
    # names such as "Sent Items" and "[Gmail]/All Mail".
    mailbox_arg = folder
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", folder):
        mailbox_arg = _quote_search_value(folder)
    status, data = client.select(mailbox_arg, readonly=True)
    if not _status_ok(status):
        detail = data[-1] if data else "unknown error"
        raise imaplib.IMAP4.error(f"Cannot select IMAP folder {folder!r}: {detail!r}")
    uidvalidity = _response_number(client, "UIDVALIDITY")
    if uidvalidity is None:
        raise imaplib.IMAP4.error(
            f"IMAP server did not return UIDVALIDITY for folder {folder!r}; "
            "an incremental sync would be unsafe."
        )
    return str(uidvalidity), _response_number(client, "HIGHESTMODSEQ"), has_condstore


def _parse_uid_search(data: Any) -> set[int]:
    values = data if isinstance(data, (list, tuple)) else [data]
    result: set[int] = set()
    for value in values:
        if not value:
            continue
        raw = value if isinstance(value, bytes) else str(value).encode()
        result.update(int(token) for token in _NUMBER_RE.findall(raw))
    return result


def _search(client: Any, *criteria: str) -> set[int]:
    status, data = client.uid("SEARCH", None, *criteria)
    if not _status_ok(status):
        detail = data[-1] if data else "unknown error"
        raise imaplib.IMAP4.error(f"UID SEARCH failed: {detail!r}")
    return _parse_uid_search(data)


def _imap_date(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d-%b-%Y")
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    raise ValueError("since must be a date, datetime, YYYY-MM-DD, or DD-Mon-YYYY")


def _quote_search_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _selected_uids(
    client: Any, *, since: date | datetime | str | None, senders: Sequence[str]
) -> set[int]:
    since_value = _imap_date(since)
    base = ("SINCE", since_value) if since_value else ("ALL",)
    if not senders:
        return _search(client, *base)

    # Searching once per sender avoids server-specific n-ary OR parsing and is
    # cheap compared with fetching message bodies.
    result: set[int] = set()
    for sender in senders:
        criteria = (*base, "FROM", _quote_search_value(sender))
        result.update(_search(client, *criteria))
    return result


def _parse_fetch_metadata(value: Any) -> tuple[int | None, tuple[str, ...], int | None]:
    raw = value if isinstance(value, bytes) else str(value).encode()
    uid_match = _UID_RE.search(raw)
    flags_match = _FLAGS_RE.search(raw)
    modseq_match = _MODSEQ_RE.search(raw)
    flags: tuple[str, ...] = ()
    if flags_match:
        flags = tuple(token.decode(errors="replace") for token in flags_match.group(1).split())
    return (
        int(uid_match.group(1)) if uid_match else None,
        flags,
        int(modseq_match.group(1)) if modseq_match else None,
    )


def _changed_uids_since(client: Any, modseq: int) -> set[int]:
    """Return UIDs whose flags changed after ``modseq`` using CONDSTORE."""
    status, data = client.uid(
        "FETCH",
        "1:*",
        "(UID FLAGS MODSEQ)",
        f"(CHANGEDSINCE {modseq})",
    )
    if not _status_ok(status):
        detail = data[-1] if data else "unknown error"
        raise imaplib.IMAP4.error(f"CONDSTORE FETCH failed: {detail!r}")
    changed: set[int] = set()
    for item in data or []:
        metadata = item[0] if isinstance(item, tuple) else item
        uid, _, _ = _parse_fetch_metadata(metadata)
        if uid is not None:
            changed.add(uid)
    return changed


def _fetch_batch(
    client: Any, uids: Sequence[int]
) -> dict[int, tuple[bytes, tuple[str, ...], int | None]]:
    if not uids:
        return {}
    sequence_set = ",".join(str(uid) for uid in uids)
    status, data = client.uid("FETCH", sequence_set, "(UID RFC822 FLAGS MODSEQ)")
    if not _status_ok(status):
        detail = data[-1] if data else "unknown error"
        raise imaplib.IMAP4.error(f"UID FETCH {sequence_set} failed: {detail!r}")

    result: dict[int, tuple[bytes, tuple[str, ...], int | None]] = {}
    missing_uid_items: list[tuple[bytes, tuple[str, ...], int | None]] = []
    for item in data or []:
        if not (isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes)):
            continue
        uid, flags, modseq = _parse_fetch_metadata(item[0])
        parsed = (item[1], flags, modseq)
        if uid is None:
            missing_uid_items.append(parsed)
        else:
            result[uid] = parsed

    # Some minimal IMAP implementations omit UID from the FETCH metadata even
    # when UID FETCH was used. The responses are ordered, so retain messages
    # rather than dropping them.
    remaining = [uid for uid in uids if uid not in result]
    for uid, parsed in zip(remaining, missing_uid_items, strict=False):
        result[uid] = parsed
    return result


def _fetch_rows(
    client: Any,
    uids: Sequence[int],
    *,
    folder: str,
    uidvalidity: str,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    for offset in range(0, len(uids), batch_size):
        batch = list(uids[offset : offset + batch_size])
        fetched = _fetch_batch(client, batch)
        for uid in batch:
            item = fetched.get(uid)
            if item is None:
                logger.warning(
                    "IMAP message %s/%s vanished before FETCH; skipping it.", folder, uid
                )
                continue
            raw, flags, modseq = item
            try:
                yield parse_message(
                    raw,
                    folder=folder,
                    uidvalidity=uidvalidity,
                    uid=uid,
                    flags=flags,
                    modseq=modseq,
                )
            except Exception as exc:  # malformed mail should not abort the mailbox
                logger.warning("Could not parse IMAP message %s/%s: %s", folder, uid, exc)
                yield {
                    "id": _message_key(folder, uidvalidity, uid),
                    "folder": folder,
                    "uidvalidity": uidvalidity,
                    "uid": uid,
                    "title": "(unparseable message)",
                    "content": raw.decode("utf-8", errors="replace"),
                    "flags": " ".join(flags),
                    "modseq": modseq,
                    "_deleted": False,
                }


def _scope_key(since: date | datetime | str | None, senders: Sequence[str]) -> str:
    return f"{_imap_date(since) or ''}|{'|'.join(sorted(set(senders)))}"


def sync_folder(
    client: Any,
    folder: str,
    state: dict[str, Any],
    *,
    since: date | datetime | str | None = None,
    senders: Sequence[str] = (),
    batch_size: int = 100,
) -> Iterator[dict[str, Any]]:
    """Sync one selected folder and update its dlt-backed state on success."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    uidvalidity, highest_modseq, has_condstore = _select_folder(client, folder)
    previous_validity = str(state.get("uidvalidity", ""))
    previous_uids = {int(uid) for uid in state.get("known_uids", [])}
    previous_highest = int(state.get("highest_uid", 0))
    previous_modseq = state.get("highest_modseq")
    scope = _scope_key(since, senders)
    validity_changed = bool(previous_validity and previous_validity != uidvalidity)
    scope_changed = state.get("scope") != scope

    # UID SEARCH ALL is intentionally done on every run. It is metadata-only,
    # cheap on IMAP servers, and is the authoritative deletion/move signal.
    current_uids = _search(client, "ALL")
    if since is None and not senders:
        selected_uids = set(current_uids)
    else:
        selected_uids = _selected_uids(client, since=since, senders=senders) & current_uids

    if validity_changed:
        deleted_uids = previous_uids
        candidates = selected_uids
        retained: set[int] = set()
    elif scope_changed:
        # Do not emit a delete and a live row for the same key in one merge.
        # Rows still selected are simply re-fetched; rows leaving the new
        # selection are tombstoned.
        deleted_uids = previous_uids - selected_uids
        candidates = selected_uids
        retained = previous_uids & selected_uids
    else:
        deleted_uids = previous_uids - current_uids
        retained = previous_uids & current_uids
        candidates = {uid for uid in selected_uids if uid > previous_highest}
        if has_condstore and previous_modseq is not None:
            changed = _changed_uids_since(client, int(previous_modseq))
            candidates.update(changed & retained)

    for uid in sorted(deleted_uids):
        yield _deleted_row(folder, previous_validity, uid)

    fetched_uids: set[int] = set()
    for row in _fetch_rows(
        client,
        sorted(candidates),
        folder=folder,
        uidvalidity=uidvalidity,
        batch_size=batch_size,
    ):
        fetched_uids.add(int(row["uid"]))
        yield row

    # State is changed only after all yields complete, so a failed extraction
    # retries from the same cursor on the next dlt run.
    state.clear()
    state.update(
        uidvalidity=uidvalidity,
        highest_uid=max(current_uids, default=0),
        known_uids=sorted(retained | fetched_uids),
        scope=scope,
    )
    if has_condstore and highest_modseq is not None:
        state["highest_modseq"] = highest_modseq


def sync_mailbox(
    client: Any,
    state: dict[str, Any],
    *,
    folders: Sequence[str] = ("INBOX",),
    since: date | datetime | str | None = None,
    senders: Sequence[str] = (),
    batch_size: int = 100,
) -> Iterator[dict[str, Any]]:
    """Sync all configured folders, including tombstones for removed folders."""
    selected_folders = list(dict.fromkeys(folders))
    if not selected_folders:
        raise ValueError("folders must contain at least one IMAP folder")
    folder_states = state.setdefault("folders", {})

    removed_folders = set(folder_states) - set(selected_folders)
    for folder in sorted(removed_folders):
        old = folder_states[folder]
        validity = str(old.get("uidvalidity", ""))
        for uid in sorted(int(item) for item in old.get("known_uids", [])):
            yield _deleted_row(folder, validity, uid)

    for folder in selected_folders:
        folder_state = folder_states.setdefault(folder, {})
        yield from sync_folder(
            client,
            folder,
            folder_state,
            since=since,
            senders=senders,
            batch_size=batch_size,
        )

    for folder in removed_folders:
        folder_states.pop(folder, None)


def build_imap_client(
    host: str,
    port: int = 993,
    *,
    tls_mode: Literal["ssl", "starttls"] = "ssl",
    ssl_context: ssl.SSLContext | None = None,
) -> imaplib.IMAP4:
    """Open a TLS-protected IMAP connection (implicit TLS or STARTTLS)."""
    context = ssl_context or ssl.create_default_context()
    if tls_mode == "ssl":
        return imaplib.IMAP4_SSL(host, port, ssl_context=context)
    if tls_mode == "starttls":
        client = imaplib.IMAP4(host, port)
        try:
            client.starttls(ssl_context=context)
        except Exception:
            try:
                client.logout()
            finally:
                raise
        return client
    raise ValueError("tls_mode must be 'ssl' or 'starttls'; unencrypted IMAP is not supported")


def imap_source(
    *,
    host: str,
    username: str,
    password: str,
    port: int = 993,
    folders: Sequence[str] | None = None,
    since: date | datetime | str | None = None,
    senders: Sequence[str] | None = None,
    batch_size: int = 100,
    tls_mode: Literal["ssl", "starttls"] = "ssl",
    ssl_context: ssl.SSLContext | None = None,
    client_factory: Any = None,
):
    """Return a dlt resource that incrementally yields IMAP messages.

    ``id`` is a lossless encoding of ``(folder, UIDVALIDITY, UID)`` and is the
    merge key. Message-ID is metadata only because it can be absent or reused.
    ``client_factory`` is an injection point for offline tests.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(
            "The IMAP connector requires dlt. Install cognee-community-connector-imap."
        ) from exc

    if not host or not username or not password:
        raise ValueError("host, username, and password are required")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    configured_folders = tuple(folders or ("INBOX",))
    configured_senders = tuple(senders or ())

    @dlt.resource(
        name="imap_messages",
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def imap_messages():
        if client_factory is None:
            client = build_imap_client(
                host,
                port,
                tls_mode=tls_mode,
                ssl_context=ssl_context,
            )
        else:
            client = client_factory(host, port)

        try:
            status, data = client.login(username, password)
            if not _status_ok(status):
                detail = data[-1] if data else "unknown error"
                raise imaplib.IMAP4.error(f"IMAP login failed: {detail!r}")
            rows = sync_mailbox(
                client,
                dlt.current.resource_state(),
                folders=configured_folders,
                since=since,
                senders=configured_senders,
                batch_size=batch_size,
            )
            yield from rows
        finally:
            try:
                client.logout()
            except Exception:
                logger.debug("IMAP logout failed", exc_info=True)

    resource = imap_messages()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "imap")
    if hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR"):
        scope = f"imap:{host}:{port}:{username}"
        setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, scope)
    return resource
