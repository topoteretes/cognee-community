"""Mailchimp connector for cognee: a ``dlt`` source that turns campaigns into memory.

Syncs a Mailchimp account's campaigns (title, subject, audience and the text of
the email) and, if asked, the notes kept on contacts, incrementally and with
forget-on-delete::

    import cognee
    from cognee_community_connector_mailchimp import mailchimp_source

    await cognee.remember(
        mailchimp_source("...-us22"),
        dataset_name="mailchimp",
        write_disposition="merge",  # required: remember() defaults to "replace"
    )

Design
------
* **One document per campaign**, primary key ``"campaign:<id>"``, and with
  ``include_notes=True`` one document per contact note,
  ``"note:<audience>:<contact>:<id>"``.
* **The email text comes from ``archive_html``.** Campaigns made in the current
  builder have no ``plain_text`` until they are sent, and the ``plain_text`` that
  appears after sending still holds raw merge tags and the footer (checked on
  a live account). ``archive_html`` has the merge tags resolved; the connector
  drops the hidden preheader, the footer block and the "View this email in
  your browser" link, and keeps the text the author wrote.
* **Change detection.** Campaigns have no "updated" field and the API filters
  only by send or create time, which miss drafts, renames and deletes. So every
  run lists every campaign (1000 per request, only the fields needed) and
  compares each one's status, send time, title, subject, preview text and
  audience with the previous run. A sent campaign cannot be edited, so its
  email text is fetched once and again only when one of those fields changes.
  Unsent campaigns (opt-in) change without moving any field, so their text is
  fetched every run and compared by content hash.
* **Notes.** Adding, editing or deleting a note never changes the contact's
  ``last_changed``, and notes have no date filter, so each run reads the notes of
  every contact in the selected audiences that has any. Contacts without a
  ``last_note`` have no notes and are skipped.
* **Deletes.** A campaign or note missing from a complete listing becomes a
  ``_deleted`` tombstone (``write_disposition="merge"`` + a ``hard_delete``
  column) and cognee's orphan cleanup forgets it. A 404 alone is never treated
  as a delete. ``/reports/{id}`` keeps answering for deleted campaigns, so it is
  never used.
* **Failure safety.** Any API error aborts the run before a tombstone is
  written, and dlt commits the sync state only after a successful load. When a
  run finds more than half of what it knows gone, it holds those deletes back
  until the next run sees the same items gone (``allow_mass_delete=True``
  forgets them at once). A run in which a listing changed while it was read
  forgets nothing, since offset paging may have skipped an item. Archiving a
  campaign never makes it forgotten.
* **Privacy.** Contact merge fields (address, phone, birthday, company) are never
  read. A note is tied to the contact's name; the email address is added only
  with ``include_contact_email=True``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Generator, Iterator
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
)

logger = get_logger("mailchimp_connector")

MAILCHIMP_SOURCE_NAME = "mailchimp"
API_URL = "https://{dc}.api.mailchimp.com/3.0"

# Tries per request for 429, 5xx and network errors (waits 2, 4, 8 and 16s).
MAX_ATTEMPTS = 5
# A longer Retry-After stops the run instead of sleeping: cognee holds its dlt
# staging lock while a source runs, and the next sync simply retries.
MAX_RETRY_AFTER_SECONDS = 60.0
# Mailchimp's largest page size for collections.
PAGE_COUNT = 1000
SENT_STATUSES = ("sent", "sending")
# "canceled" and "canceling" are sends stopped part way (documented statuses).
UNSENT_STATUSES = ("save", "paused", "schedule", "canceled", "canceling")
# Archiving hides a campaign in Mailchimp but deletes nothing, so an archived
# campaign is kept: as sent when it has a send time, otherwise as unsent.
ARCHIVED = "archived"
# A run finding more than this share of the synced items gone, and at least
# MASS_DELETE_MIN_ITEMS of them, holds the deletes back for one run. Our own
# choice: small enough to catch a wrong audience or folder selection, large
# enough that ordinary cleanups never wait.
DEFAULT_MAX_DELETE_RATIO = 0.5
MASS_DELETE_MIN_ITEMS = 10

# An API key is 32 hex characters, a dash and the data centre (for example us22).
_API_KEY = re.compile(r"^[0-9a-f]{32}-([a-z]{2,}[0-9]+)$")
# Record ids in a path; a contact's id is the MD5 of its email address.
_PATH_ID = re.compile(r"\b(campaigns|lists|members|notes)/[^/]+")

CAMPAIGN_FIELDS = ",".join(
    [
        "total_items",
        "campaigns.id",
        "campaigns.status",
        "campaigns.send_time",
        "campaigns.archive_url",
        "campaigns.long_archive_url",
        "campaigns.recipients.list_id",
        "campaigns.recipients.list_name",
        "campaigns.settings.title",
        "campaigns.settings.subject_line",
        "campaigns.settings.preview_text",
        "campaigns.settings.folder_id",
    ]
)
# Never merge fields (address, phone, birthday); the email address only when a
# note should carry it (include_contact_email).
MEMBER_FIELDS = "total_items,members.id,members.full_name,members.last_note"
NOTE_FIELDS = "total_items,notes.id,notes.note,notes.created_at,notes.updated_at,notes.created_by"


class MailchimpError(RuntimeError):
    """Base class for Mailchimp API errors raised by this connector."""


class MailchimpAuthError(MailchimpError):
    """The API key was rejected (401), or belongs to another data centre (403)."""


class MailchimpForbiddenError(MailchimpError):
    """403: the key may not read this resource."""


class MailchimpNotFoundError(MailchimpError):
    """404: the item was deleted or does not exist. Never retried."""


class MailchimpRateLimitError(MailchimpError):
    """429 that did not clear after retrying, or asked for too long a wait."""


def _endpoint(path: str) -> str:
    """The path with record ids replaced, for logs and error messages."""
    return _PATH_ID.sub(r"\1/{id}", path.lstrip("/")) or "/"


def data_centre(api_key: str) -> str:
    """The data centre a key belongs to (the part after the dash)."""
    match = _API_KEY.match(api_key.strip())
    if not match:
        raise ValueError(
            "That does not look like a Mailchimp API key "
            "(32 hex characters, a dash and the data centre, e.g. ...-us22)."
        )
    return match.group(1)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class MailchimpClient:
    """Small Mailchimp Marketing API client: Basic auth, offset paging, retries.

    The host is built only from the key's data centre, never from user input.
    ``http_client`` is an injection point for tests (``httpx.MockTransport``).
    The key is never logged or put into an exception message.
    """

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.dc = data_centre(api_key)
        self.base_url = API_URL.format(dc=self.dc)
        self._auth = httpx.BasicAuth("cognee", api_key.strip())
        self.http = http_client or httpx.Client(timeout=30.0)
        self._sleep = sleep or time.sleep

    def request(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        endpoint = _endpoint(path)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.http.get(url, params=params, auth=self._auth)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise MailchimpError(
                        f"Mailchimp API unreachable on {endpoint}: {type(exc).__name__}"
                    ) from None
                self._sleep(float(2**attempt))
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                error = MailchimpRateLimitError if status == 429 else MailchimpError
                if attempt == MAX_ATTEMPTS:
                    raise error(
                        f"Mailchimp API returned {status} on {endpoint} after {attempt} tries"
                    )
                delay = _retry_delay(response, attempt)
                if delay > MAX_RETRY_AFTER_SECONDS:
                    raise error(
                        f"Mailchimp API returned {status} on {endpoint} and asked to wait "
                        f"{delay:.0f}s; stopping so the next sync can retry."
                    )
                logger.warning(
                    "Mailchimp: %s on %s, retrying in %.1fs (%d/%d).",
                    status,
                    endpoint,
                    delay,
                    attempt,
                    MAX_ATTEMPTS,
                )
                self._sleep(delay)
                continue
            if status >= 400:
                raise _error_for(response, endpoint)
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                # An empty or odd 200 must never read as "nothing there".
                raise MailchimpError(f"Mailchimp API returned an unexpected body on {endpoint}")
            return body

    def iter_collection(self, path: str, key: str, params: dict[str, Any]) -> Listing:
        return Listing(self, path, key, params)

    def get_account(self) -> dict:
        return self.request("", {"fields": "account_id,account_name"})

    def list_campaigns(self) -> Listing:
        # Oldest first, so a campaign created while paging lands on a later page
        # instead of shifting the offsets of the ones already read.
        return self.iter_collection(
            "campaigns",
            "campaigns",
            {"fields": CAMPAIGN_FIELDS, "sort_field": "create_time", "sort_dir": "ASC"},
        )

    def get_content(self, campaign_id: str) -> dict:
        return self.request(
            f"campaigns/{campaign_id}/content", {"fields": "archive_html,plain_text"}
        )

    def list_audiences(self) -> Listing:
        return self.iter_collection("lists", "lists", {"fields": "total_items,lists.id,lists.name"})

    def list_members(self, list_id: str, *, with_email: bool = False) -> Listing:
        fields = MEMBER_FIELDS + (",members.email_address" if with_email else "")
        return self.iter_collection(f"lists/{list_id}/members", "members", {"fields": fields})

    def list_notes(self, list_id: str, member_id: str) -> Listing:
        return self.iter_collection(
            f"lists/{list_id}/members/{member_id}/notes",
            "notes",
            {"fields": NOTE_FIELDS, "sort_field": "created_at", "sort_dir": "ASC"},
        )


class Listing:
    """Every item of a collection, read with ``count``/``offset`` paging.

    ``moved`` turns true when the collection's total changed between pages. An
    item deleted mid-read shifts the later ones back by one, so one of them is
    never listed, and such a listing cannot prove that anything is gone.
    """

    def __init__(self, client: MailchimpClient, path: str, key: str, params: dict[str, Any]):
        self._client = client
        self._path = path
        self._key = key
        self._params = params
        self.moved = False

    def __iter__(self) -> Iterator[dict]:
        self.moved = False
        offset = 0
        total: int | None = None
        while True:
            page = self._client.request(
                self._path, {**self._params, "count": PAGE_COUNT, "offset": offset}
            )
            items = page.get(self._key)
            if not isinstance(items, list) or not isinstance(page.get("total_items"), int):
                # Without both, an incomplete listing would look complete and its
                # missing items would be forgotten.
                raise MailchimpError(
                    f"Mailchimp API returned an unexpected page on {_endpoint(self._path)}; "
                    "nothing was synced or forgotten."
                )
            if total is not None and page["total_items"] != total:
                self.moved = True
            total = page["total_items"]
            yield from items
            offset += len(items)
            if not items and offset < total:
                self.moved = True  # the collection shrank under the offset
            if not items or offset >= total:
                return


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return float(2**attempt)


def _error_for(response: httpx.Response, endpoint: str) -> MailchimpError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    title = body.get("title") if isinstance(body, dict) else None
    detail = body.get("detail") if isinstance(body, dict) else None
    message = f"Mailchimp API returned {response.status_code} on {endpoint}"
    if title or detail:
        message += f" ({title}: {detail})"
    if response.status_code == 401:
        return MailchimpAuthError(message + ". Check the API key.")
    if response.status_code == 403:
        if detail and "datacenter" in detail.lower():
            return MailchimpAuthError(message)
        return MailchimpForbiddenError(message)
    if response.status_code == 404:
        return MailchimpNotFoundError(message)
    return MailchimpError(message)


# ---------------------------------------------------------------------------
# Email text
# ---------------------------------------------------------------------------
# Formatting characters that never carry meaning: soft hyphen, zero-width space,
# word joiner, BOM. Zero-width (non-)joiners stay: Indic scripts and emoji
# sequences need them. The preheader's padding characters are dropped with the
# hidden preheader itself.
_INVISIBLE = re.compile(r"[\u00ad\u200b\u2060\ufeff]+")
_MERGE_TAG = re.compile(r"\*\|[^|]*\|\*")
# Any run of whitespace except a newline (covers non-breaking and figure spaces).
_SPACES = re.compile(r"[^\S\n]+")
_LINE_BREAK = re.compile(r"<br\s*/?>\r?\n?", re.IGNORECASE)
_TAG_OTHER_THAN_BR = re.compile(r"<(?!br\s*/?>)[a-zA-Z/!]", re.IGNORECASE)
_SKIPPED_TAGS = {"style", "script", "title", "head"}
_BLOCK_TAGS = {
    "p",
    "div",
    "td",
    "tr",
    "li",
    "ul",
    "ol",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "section",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}
# Footer containers, matched in class and id: "mceFooterSection" is the current
# builder's (seen live); "templateFooter" and "footerContainer" are the classic
# templates' footer id and class.
_FOOTER_MARKERS = ("mcefootersection", "templatefooter", "footercontainer")


class _EmailText(HTMLParser):
    """Visible text of a campaign email, without preheader, footer or view link."""

    def __init__(self, archive_urls: tuple[str, ...]):
        super().__init__(convert_charrefs=True)
        self.archive_urls = archive_urls
        self.parts: list[str] = []
        self.hidden: list[bool] = []

    def _is_hidden(self, tag: str, attrs: dict[str, str]) -> bool:
        if tag in _SKIPPED_TAGS:
            return True
        marker = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
        if any(name in marker for name in _FOOTER_MARKERS):
            return True
        style = attrs.get("style", "").replace(" ", "").lower()
        if "display:none" in style:
            return True  # the hidden preheader
        if tag == "a":
            href = attrs.get("href", "")
            # "View this email in your browser": the archive page before sending
            # (campaign-archive.com/?e=[UNIQID]...) and after (mailchi.mp/...?e=[UNIQID]).
            if "[UNIQID]" in href or "*|ARCHIVE|*" in href:
                return True
            if any(url and href.startswith(url) for url in self.archive_urls):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS or tag == "br":
            self.parts.append("\n")
        if tag in _VOID_TAGS:
            return
        parent_hidden = bool(self.hidden and self.hidden[-1])
        self.hidden.append(parent_hidden or self._is_hidden(tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if self.hidden:
            self.hidden.pop()
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden and self.hidden[-1]:
            return
        self.parts.append(data)


def _tidy(text: str) -> str:
    text = _MERGE_TAG.sub("", _INVISIBLE.sub("", text))
    lines = [_SPACES.sub(" ", line).strip() for line in text.replace("\r", "").split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _plain_text_body(plain_text: str) -> str:
    """Best effort for a campaign with only ``plain_text``: drop the view link,
    image alt lines and everything from the footer's copyright line on."""
    kept = []
    for line in _MERGE_TAG.sub("", plain_text).replace("\r", "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("Copyright (C)"):
            break
        if stripped.startswith("View this email in your browser") or stripped == "Logo":
            continue
        kept.append(line.replace("()", ""))
    return _tidy("\n".join(kept))


def email_text(content: dict, archive_urls: tuple[str, ...] = ()) -> str:
    """The text the author wrote, from ``archive_html`` (``plain_text`` as a fallback)."""
    archive_html = content.get("archive_html") or ""
    if archive_html.strip():
        if not _TAG_OTHER_THAN_BR.search(archive_html):
            # Checked live: after Mailchimp halted a send, archive_html became
            # the plain text (merge tags resolved) with one <br /> per line.
            return _plain_text_body(unescape(_LINE_BREAK.sub("\n", archive_html)))
        parser = _EmailText(tuple(u for u in archive_urls if u))
        parser.feed(archive_html)
        parser.close()
        return _tidy("".join(parser.parts))
    return _plain_text_body(content.get("plain_text") or "")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _date(value: Any) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _was_sent(campaign: dict) -> bool:
    status = campaign.get("status")
    return status in SENT_STATUSES or (status == ARCHIVED and bool(campaign.get("send_time")))


def _status_line(campaign: dict) -> str:
    status = campaign.get("status")
    date = _date(campaign.get("send_time"))
    if _was_sent(campaign):
        line = f"Sent on {date}" if date else "Sent"
        return f"{line}; archived in Mailchimp" if status == ARCHIVED else line
    # Checked live: when Mailchimp halts a send (account under review), the
    # campaign goes back to "save" but keeps its send_time. A canceled send
    # keeps it too.
    if status in ("save", "canceled", "canceling") and date:
        state = "draft" if status == "save" else "canceled"
        return f"Status: {state}; a send started on {date} was stopped"
    return {
        "save": "Status: draft, not sent",
        "schedule": "Status: scheduled, not sent yet",
        "paused": "Status: paused",
        "canceled": "Status: canceled, not sent",
        "canceling": "Status: canceled, not sent",
        ARCHIVED: "Status: archived in Mailchimp, not sent",
    }.get(status, f"Status: {status}")


def campaign_title(campaign: dict) -> str:
    return ((campaign.get("settings") or {}).get("title") or "").strip() or "(untitled)"


def render_campaign(campaign: dict, body: str) -> str:
    settings = campaign.get("settings") or {}
    recipients = campaign.get("recipients") or {}
    lines = [f"Mailchimp campaign: {campaign_title(campaign)}"]
    if settings.get("subject_line"):
        lines.append(f"Subject: {settings['subject_line'].strip()}")
    if settings.get("preview_text"):
        lines.append(f"Preview text: {settings['preview_text'].strip()}")
    if recipients.get("list_name"):
        lines.append(f"Audience: {recipients['list_name']}")
    lines.append(_status_line(campaign))
    if body:
        lines += ["", body]
    return "\n".join(lines)


def render_note(
    note: dict, member: dict, audience_name: str, include_contact_email: bool = False
) -> str:
    contact = (member.get("full_name") or "").strip() or "an unnamed contact"
    if include_contact_email and member.get("email_address"):
        contact += f" <{member['email_address']}>"
    lines = [f"Mailchimp contact note about {contact}"]
    if audience_name:
        lines.append(f"Audience: {audience_name}")
    written = f"Written by {note.get('created_by') or 'unknown'}"
    if _date(note.get("created_at")):
        written += f" on {_date(note.get('created_at'))}"
    lines += [written, "", (note.get("note") or "").strip()]
    return "\n".join(lines)


def node_set(list_id: str | None) -> list[str]:
    return [f"{MAILCHIMP_SOURCE_NAME}:{list_id or 'no-audience'}"]


def _digest(row: dict) -> str:
    payload = "\x1f".join(
        [row["title"], row["content"], row.get("url") or "", ",".join(row[NODE_SET_COLUMN])]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _signal(campaign: dict) -> list:
    """The listed fields whose change means the campaign must be read again."""
    settings = campaign.get("settings") or {}
    recipients = campaign.get("recipients") or {}
    # "sending" turns into "sent" with the same text; archiving changes the
    # status line, so it stays its own value.
    status = "sent" if campaign.get("status") in SENT_STATUSES else campaign.get("status")
    return [
        status,
        campaign.get("send_time") or "",
        settings.get("title") or "",
        settings.get("subject_line") or "",
        settings.get("preview_text") or "",
        recipients.get("list_id") or "",
        recipients.get("list_name") or "",
    ]


def _selected(campaign: dict, audience_ids, folder_ids, include_unsent: bool) -> bool:
    unsent = campaign.get("status") in (*UNSENT_STATUSES, ARCHIVED)
    if not _was_sent(campaign) and not (include_unsent and unsent):
        return False
    if audience_ids and (campaign.get("recipients") or {}).get("list_id") not in audience_ids:
        return False
    return not (folder_ids and (campaign.get("settings") or {}).get("folder_id") not in folder_ids)


def _reset_stats(stats: dict[str, int]) -> None:
    stats.clear()
    stats.update(
        scanned=0,
        changed=0,
        unchanged=0,
        deleted=0,
        skipped=0,
        held_back=0,
        failed=0,
        failed_auth=0,
        failed_rate_limit=0,
    )


def _iter_rows(
    client: MailchimpClient,
    *,
    state: dict,
    stats: dict[str, int],
    audience_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    include_unsent: bool = False,
    include_notes: bool = False,
    include_contact_email: bool = False,
    max_delete_ratio: float = DEFAULT_MAX_DELETE_RATIO,
    allow_mass_delete: bool = False,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed campaign and note rows, then tombstones for what vanished.

    ``state["items"]`` maps each row id to its fingerprint from the last run:
    ``[*signal, digest]`` for a campaign and ``[updated_at, digest]`` for a note.
    Any API error propagates and aborts the run before a tombstone is yielded;
    dlt then keeps the old state.
    """
    _reset_stats(stats)
    previous: dict[str, list] = dict(state.get("items") or {})
    present: dict[str, list] = {}

    campaigns = client.list_campaigns()
    for campaign in campaigns:
        if check_active is not None:
            check_active()
        if not _selected(campaign, audience_ids, folder_ids, include_unsent):
            continue
        stats["scanned"] += 1
        key = f"campaign:{campaign['id']}"
        signal = _signal(campaign)
        old = previous.get(key)
        sent = _was_sent(campaign)
        # A sent campaign cannot be edited: its text only needs reading again
        # when something about it changed. Drafts change silently, so read them.
        if sent and old and old[:-1] == signal:
            stats["unchanged"] += 1
            present[key] = old
            continue
        try:
            content = client.get_content(campaign["id"])
        except MailchimpNotFoundError:
            # Deleted between the listing and this call; the next listing decides.
            stats["skipped"] += 1
            if old:
                present[key] = old
            continue
        urls = (campaign.get("archive_url") or "", campaign.get("long_archive_url") or "")
        row = {
            "id": key,
            "title": campaign_title(campaign),
            "content": render_campaign(campaign, email_text(content, urls)),
            "url": campaign.get("long_archive_url") or campaign.get("archive_url") or None,
            "_deleted": False,
            NODE_SET_COLUMN: node_set((campaign.get("recipients") or {}).get("list_id")),
        }
        digest = _digest(row)
        present[key] = [*signal, digest]
        if old and old[-1] == digest:
            stats["unchanged"] += 1
            continue
        stats["changed"] += 1
        yield row

    moved = campaigns.moved
    if include_notes:
        notes_moved = yield from _iter_note_rows(
            client, audience_ids, include_contact_email, previous, present, stats, check_active
        )
        moved = moved or notes_moved

    gone = sorted(set(previous) - set(present))
    large = len(gone) >= MASS_DELETE_MIN_ITEMS and len(gone) > max_delete_ratio * len(previous)
    if gone and moved:
        # A listing changed while it was read, so an item may have been skipped.
        # This run forgets nothing; the next one decides.
        logger.warning(
            "Mailchimp: a listing changed while it was read; holding %d delete(s) until "
            "the next sync.",
            len(gone),
        )
        present.update({key: previous[key] for key in gone})
        stats["held_back"] = len(gone)
        gone = []
    elif large and not allow_mass_delete and gone != state.get("pending_delete"):
        # A wrong audience or folder selection looks exactly like a mass delete.
        # Hold the deletes back and forget them only when the next run sees the
        # same items gone, so a scheduled sync never needs a manual override.
        logger.warning(
            "Mailchimp: %d of %d synced items are gone; holding the deletes until the "
            "next sync confirms them.",
            len(gone),
            len(previous),
        )
        present.update({key: previous[key] for key in gone})
        state["pending_delete"] = gone
        stats["held_back"] = len(gone)
        gone = []
    else:
        state.pop("pending_delete", None)
    for key in gone:
        stats["deleted"] += 1
        yield {"id": key, "_deleted": True}

    state["items"] = present
    logger.info(
        "Mailchimp: scanned %d item(s): %d changed, %d unchanged, %d skipped; "
        "%d deletion(s), %d held back.",
        stats["scanned"],
        stats["changed"],
        stats["unchanged"],
        stats["skipped"],
        stats["deleted"],
        stats["held_back"],
    )


def _iter_note_rows(
    client: MailchimpClient,
    audience_ids: list[str] | None,
    include_contact_email: bool,
    previous: dict[str, list],
    present: dict[str, list],
    stats: dict[str, int],
    check_active: Callable[[], None] | None,
) -> Generator[dict, None, bool]:
    """Yield changed note rows; return whether any listing moved while read."""
    audiences = client.list_audiences()
    names = {a["id"]: a.get("name") or "" for a in audiences}
    moved = audiences.moved
    if audience_ids:
        missing = [a for a in audience_ids if a not in names]
        if missing:
            # A selected audience this key cannot see is a configuration problem,
            # never proof that its notes were deleted.
            raise MailchimpNotFoundError(
                f"Mailchimp audience(s) {missing} are not visible to this key. "
                "Nothing was synced or forgotten."
            )
    selected = audience_ids or list(names)

    for list_id in selected:
        members = client.list_members(list_id, with_email=include_contact_email)
        for member in members:
            if check_active is not None:
                check_active()
            # Checked live: a contact without notes has no last_note at all.
            if not member.get("last_note"):
                continue
            prefix = f"note:{list_id}:{member['id']}:"
            try:
                listing = client.list_notes(list_id, member["id"])
                notes = list(listing)
                moved = moved or listing.moved
            except MailchimpNotFoundError:
                # The contact went away between the two listings. The next run
                # decides; until then keep the notes already synced for it.
                stats["scanned"] += 1
                stats["skipped"] += 1
                present.update({k: v for k, v in previous.items() if k.startswith(prefix)})
                continue
            for note in notes:
                stats["scanned"] += 1
                key = f"{prefix}{note['id']}"
                old = previous.get(key)
                row = {
                    "id": key,
                    "title": f"Note about {(member.get('full_name') or '').strip() or 'a contact'}",
                    "content": render_note(
                        note, member, names.get(list_id, ""), include_contact_email
                    ),
                    "url": None,
                    "_deleted": False,
                    NODE_SET_COLUMN: node_set(list_id),
                }
                digest = _digest(row)
                present[key] = [note.get("updated_at") or "", digest]
                if old and old[-1] == digest:
                    stats["unchanged"] += 1
                    continue
                stats["changed"] += 1
                yield row
        moved = moved or members.moved
    return moved


def _id_list(name: str, values: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        # A bare string would be read one character at a time.
        raise TypeError(f"{name} takes a list of ids, for example [{values!r}].")
    return [str(v).strip() for v in values if str(v).strip()] or None


def mailchimp_source(
    api_key: str,
    *,
    audience_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    include_unsent: bool = False,
    include_notes: bool = False,
    include_contact_email: bool = False,
    max_delete_ratio: float = DEFAULT_MAX_DELETE_RATIO,
    allow_mass_delete: bool = False,
    resource_name: str | None = None,
    service: httpx.Client | None = None,
    http_client: httpx.Client | None = None,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource with one row per Mailchimp campaign (and note).

    Hand it to ``cognee.remember(...)`` or ``cognee.add(...)`` with
    ``write_disposition="merge"``.

    Args:
        api_key: Mailchimp Marketing API key (it ends with the data centre, such
            as ``-us22``).
        audience_ids: Only campaigns sent to these audiences (and, with
            ``include_notes``, only these audiences' notes). Default: all.
        folder_ids: Only campaigns in these campaign folders. Default: all.
        include_unsent: Also sync drafts and scheduled, paused, canceled and
            unsent archived campaigns. Off by default: unfinished drafts often
            still hold template placeholder text.
        include_notes: Also sync the notes kept on contacts. Costs one request per
            contact that has notes, every run.
        include_contact_email: Add the contact's email address to a note. Off by
            default; the note is tied to the contact's name.
        max_delete_ratio: When a run finds more than this share of the synced
            items gone (and at least 10 of them), it holds the deletes back until
            the next run sees the same items gone.
        allow_mass_delete: Forget such a batch at once, for an intended cleanup.
        resource_name: dlt resource name, also the staging table and the key of
            the sync state. Default ``mailchimp_<data centre>``.
        service: Pre-built ``httpx`` client (the name cognee's own connectors
            use); ``http_client`` is the same thing. Mainly for tests.
        check_active: Optional callable a host uses to stop a running sync; it
            raises to abort.

    ``cognee_sync_stats`` on the returned resource holds counts only: scanned,
    changed, unchanged, deleted, skipped, held_back, and failed, failed_auth,
    failed_rate_limit (set to 1 when a run stops on that kind of error).
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Mailchimp connector needs a cognee build that reads per-row node sets "
            "(DOCUMENT_SYNC_VERSION >= 2, cognee 1.6.3 or later)."
        )
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("A Mailchimp API key is required.")
    key = api_key.strip()
    dc = data_centre(key)
    if service is not None and http_client is not None and service is not http_client:
        raise ValueError("Pass service or http_client, not both.")
    injected = service or http_client
    audiences = _id_list("audience_ids", audience_ids)
    folders = _id_list("folder_ids", folder_ids)
    if not 0 < max_delete_ratio <= 1:
        raise ValueError("max_delete_ratio must be above 0 and at most 1.")

    name = resource_name or f"mailchimp_{dc}"
    stats: dict[str, int] = {}
    _reset_stats(stats)

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}, **NODE_SET_COLUMN_HINT},
    )
    def mailchimp_items():
        _reset_stats(stats)
        owned = None if injected is not None else httpx.Client(timeout=30.0)
        try:
            client = MailchimpClient(key, http_client=injected or owned)
            account_id = str(client.get_account().get("account_id") or "")
            if not account_id:
                raise MailchimpError("Mailchimp API returned no account id; nothing was synced.")
            state = dlt.current.resource_state()
            known = state.get("account_id")
            if state.get("items") and known not in (None, account_id):
                # Same resource name, other account: syncing on would forget
                # everything the other account synced.
                raise ValueError(
                    f"Mailchimp resource {name!r} already holds another account's items. "
                    "Give each Mailchimp source its own resource_name."
                )
            state["account_id"] = account_id
            yield from dlt_utils.guarded_rows(
                _iter_rows(
                    client,
                    state=state,
                    stats=stats,
                    audience_ids=audiences,
                    folder_ids=folders,
                    include_unsent=include_unsent,
                    include_notes=include_notes,
                    include_contact_email=include_contact_email,
                    max_delete_ratio=max_delete_ratio,
                    allow_mass_delete=allow_mass_delete,
                    check_active=check_active,
                ),
                check_active,
            )
        except Exception as exc:
            # Categories only, never the message: a host shows these counts.
            stats["failed"] = 1
            if isinstance(exc, MailchimpAuthError):
                stats["failed_auth"] = 1
            elif isinstance(exc, MailchimpRateLimitError):
                stats["failed_rate_limit"] = 1
            raise
        finally:
            if owned is not None:
                owned.close()

    resource = mailchimp_items()
    # Each row (id/title/content/url plus cognee_node_set) becomes a text
    # document that goes through normal cognify.
    setattr(resource, DOCUMENT_SOURCE_ATTR, MAILCHIMP_SOURCE_NAME)
    # Own dlt pipeline per account, so the sync state never mixes with other
    # dlt sources (otherwise all of them share "ingest_dlt_source").
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    # Counts only, never titles or content.
    resource.cognee_sync_stats = stats
    return resource
