"""Parse WhatsApp "Export chat" files into messages.

WhatsApp exports a chat as plain text: a ``.txt`` on Android, or a ``.zip``
holding ``_chat.txt`` on iPhone. Every message starts with a timestamp line and
may continue over the following lines. The two platforms write that line
differently, and the date order follows the phone's locale::

    Android  31/12/23, 9:15 pm - Asha: text
    iPhone   [31/12/23, 9:15:42 PM] Asha: text

This module knows nothing about cognee or dlt. It only turns a file into a
:class:`ChatExport` so the rest of the connector can work with plain data.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

# Invisible direction marks and spaces that WhatsApp sprinkles into exports:
# U+200E/U+200F before iPhone attachment lines and around names, U+202A-U+202E
# around phone numbers, U+202F (narrow no-break space) before AM/PM on newer
# Android versions, and U+00A0 in some locales.
_INVISIBLE = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff"), None)
_SPACES = {ord("\u202f"): " ", ord("\u00a0"): " "}

_DATE = r"\d{1,4}[./\-]\d{1,2}[./\-]\d{1,4}"
_TIME = r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[AaPp]\.?\s?[Mm]\.?)?"
# "[31/12/23, 9:15:42 PM] rest" (iPhone) and "31/12/23, 9:15 pm - rest" (Android).
_IPHONE_LINE = re.compile(rf"^\[(?P<date>{_DATE}),?\s(?P<time>{_TIME})\]\s(?P<rest>.*)$")
_ANDROID_LINE = re.compile(rf"^(?P<date>{_DATE}),?\s(?P<time>{_TIME})\s[-\u2013]\s(?P<rest>.*)$")
# 21:15, 21.15, 9:15:42 pm, 9:15 p.m.
_TIME_PARTS = re.compile(
    r"^(?P<h>\d{1,2})[:.](?P<m>\d{2})(?:[:.](?P<s>\d{2}))?(?:\s?(?P<ap>[AaPp])\.?\s?[Mm]\.?)?$"
)

# Chat file names WhatsApp uses for the export, so the chat gets a readable name.
_FILE_NAME_PREFIXES = ("WhatsApp Chat with ", "WhatsApp Chat - ")

_DELETED = {
    "this message was deleted",
    "this message was deleted.",
    "you deleted this message",
    "you deleted this message.",
}
_EDITED_SUFFIX = re.compile(r"\s*<This message was edited>\s*$")
# iPhone media placeholders ("image omitted") and the Android one ("<Media omitted>").
_OMITTED = re.compile(
    r"^(?P<kind>image|video|audio|sticker|GIF|document|Contact card) omitted$", re.IGNORECASE
)
_ATTACHED_IPHONE = re.compile(r"^<attached: (?P<name>[^>]+)>$")
_ATTACHED_ANDROID = re.compile(r"^(?P<name>\S+\.\w{2,5}) \(file attached\)$")


@dataclass(frozen=True)
class Message:
    """One message or system event, in the exporting phone's local time."""

    sent_at: datetime
    sender: str | None  # None for system events ("Asha added Ravi")
    text: str


@dataclass
class ChatExport:
    """A parsed export: the chat's name and its messages in file order."""

    chat_name: str
    messages: list[Message] = field(default_factory=list)
    unparsed_lines: int = 0  # lines before the first timestamp that were dropped
    date_order: str = "dmy"  # the order used to read the dates
    ambiguous_date_order: bool = False  # no date could tell day and month apart

    def days(self) -> dict[date, list[Message]]:
        """Messages grouped by calendar day, in file order."""
        grouped: dict[date, list[Message]] = {}
        for message in self.messages:
            grouped.setdefault(message.sent_at.date(), []).append(message)
        return grouped


class ExportFormatError(ValueError):
    """The file is not a WhatsApp chat export this parser can read."""


def read_export(path: str | Path, date_order: str | None = None) -> ChatExport:
    """Read a ``.txt`` export or a ``.zip`` holding one and parse it.

    ``date_order`` ("dmy", "mdy" or "ymd") overrides detection. Leave it unset
    unless a file only has dates where day and month are both 12 or less.
    """
    path = Path(path)
    if path.suffix.lower() == ".zip":
        text = _read_zip(path)
    else:
        text = path.read_text(encoding="utf-8-sig")
    return parse_export(text, chat_name=_chat_name(path), date_order=date_order)


def parse_export(text: str, chat_name: str, date_order: str | None = None) -> ChatExport:
    """Parse the text of an export. Raises :class:`ExportFormatError` if no line matches."""
    lines = [_clean(line) for line in text.splitlines()]
    headers = [(i, _match(line)) for i, line in enumerate(lines)]
    starts = [(i, m) for i, m in headers if m is not None]
    if not starts:
        raise ExportFormatError("no WhatsApp message lines found")

    if date_order is None:
        order, ambiguous = _detect_date_order(m.group("date") for _, m in starts)
    else:
        if date_order not in ("dmy", "mdy", "ymd"):
            raise ValueError('date_order must be "dmy", "mdy" or "ymd"')
        order, ambiguous = date_order, False

    export = ChatExport(chat_name=chat_name, date_order=order, ambiguous_date_order=ambiguous)
    export.unparsed_lines = starts[0][0]  # anything above the first timestamp line

    # Each message runs from its timestamp line up to the next one, so lines
    # without a timestamp are continuations of a multi-line message.
    for n, (index, match) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body = "\n".join([match.group("rest"), *lines[index + 1 : end]]).strip()
        sent_at = datetime.combine(
            _parse_date(match.group("date"), order), _parse_time(match.group("time"))
        )
        message = _message(sent_at, body)
        if message is not None:
            export.messages.append(message)
    return export


def _read_zip(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".txt")]
        if not names:
            raise ExportFormatError("zip has no .txt chat file")
        # iPhone names the chat file _chat.txt; otherwise take the only .txt.
        chosen = next((n for n in names if Path(n).name == "_chat.txt"), None)
        if chosen is None:
            if len(names) > 1:
                raise ExportFormatError("zip has several .txt files and no _chat.txt")
            chosen = names[0]
        return archive.read(chosen).decode("utf-8-sig")


def _chat_name(path: Path) -> str:
    stem = path.name
    for suffix in (".zip", ".txt"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    for prefix in _FILE_NAME_PREFIXES:
        if stem.startswith(prefix):
            return stem[len(prefix) :].strip() or stem
    return stem


def _clean(line: str) -> str:
    return line.translate(_INVISIBLE).translate(_SPACES).rstrip()


def _match(line: str) -> re.Match[str] | None:
    return _IPHONE_LINE.match(line) or _ANDROID_LINE.match(line)


def _split_date(value: str) -> tuple[str, str, str]:
    parts = re.split(r"[./\-]", value)
    if len(parts) != 3:
        raise ExportFormatError("date is not three numbers")
    return parts[0], parts[1], parts[2]


def _detect_date_order(values: Iterable[str]) -> tuple[str, bool]:
    """Pick the date order from the whole file.

    A four-digit first part means year first. Otherwise a first part above 12
    can only be a day and a second part above 12 can only be a day. When no
    date settles it, fall back to day first and report it as ambiguous.
    """
    day_first = month_first = False
    for value in values:
        first, second, _ = _split_date(value)
        if len(first) == 4:
            return "ymd", False
        if int(first) > 12:
            day_first = True
        if int(second) > 12:
            month_first = True
    if day_first and month_first:
        raise ExportFormatError("dates disagree on day and month order")
    if month_first:
        return "mdy", False
    return "dmy", not day_first


def _parse_date(value: str, order: str) -> date:
    first, second, third = _split_date(value)
    if order == "ymd":
        year, month, day = first, second, third
    elif order == "mdy":
        month, day, year = first, second, third
    else:
        day, month, year = first, second, third
    full_year = int(year) + 2000 if len(year) == 2 else int(year)
    return date(full_year, int(month), int(day))


def _parse_time(value: str) -> time:
    match = _TIME_PARTS.match(value.strip())
    if match is None:
        raise ExportFormatError("time is not in a known format")
    hour, minute = int(match.group("h")), int(match.group("m"))
    second = int(match.group("s") or 0)
    meridiem = (match.group("ap") or "").lower()
    if meridiem == "p" and hour != 12:
        hour += 12
    elif meridiem == "a" and hour == 12:
        hour = 0
    return time(hour, minute, second)


def _message(sent_at: datetime, body: str) -> Message | None:
    """Turn a line's body into a message, a system event, or nothing (deleted)."""
    sender, sep, text = body.partition(": ")
    # System events have no "Name: " prefix. A quote before the colon means the
    # colon belongs to quoted text ('Asha changed the subject to "Trip: Goa"').
    if not sep or '"' in sender or "\u201c" in sender or not sender.strip():
        return Message(sent_at=sent_at, sender=None, text=body)

    text = _EDITED_SUFFIX.sub("", text).strip()
    if text.lower() in _DELETED:
        return None
    return Message(sent_at=sent_at, sender=sender.strip(), text=_media_marker(text))


def _media_marker(text: str) -> str:
    if text == "<Media omitted>":
        return "[media]"
    if (m := _OMITTED.match(text)) is not None:
        return f"[{m.group('kind').lower()}]"
    if (m := _ATTACHED_IPHONE.match(text)) is not None:
        return f"[attachment: {m.group('name')}]"
    if (m := _ATTACHED_ANDROID.match(text)) is not None:
        return f"[attachment: {m.group('name')}]"
    return text
