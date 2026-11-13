"""Shared fakes for the Mailchimp connector tests (no network, no credentials).

``FakeMailchimp`` is an in-memory stand-in for the parts of the Mailchimp
Marketing API the connector uses. Every behaviour below was observed on a live
free account (data centre us22):

* the key is sent with HTTP Basic auth; a bad key gives 401 "API Key Invalid",
  and a key used on another data centre's host gives 403 "...linked to a
  different datacenter";
* collections page with ``count``/``offset`` and report ``total_items``;
* a campaign has no "updated" field; editing a draft changes only its content;
* ``/content`` returns ``archive_html`` and ``html``; ``plain_text`` appears only
  after the campaign is sent, and still holds merge tags and the footer;
* sending moves status to "sending" and then "sent", sets ``send_time`` and
  changes the "View this email in your browser" link from
  ``campaign-archive.com/?e=[UNIQID]...`` to ``mailchi.mp/<slug>?e=[UNIQID]``;
* a rename only changes ``settings.title``;
* a deleted campaign is gone from every listing and returns 404;
* notes live under each contact; adding, editing or deleting one never changes
  the contact's ``last_changed``; a contact has ``last_note`` only while it has
  notes; a deleted note returns 404.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest

API_KEY = "0123456789abcdef" * 2 + "-us22"  # built, so no key-shaped literal
LIST_ID = "aaaaaaaaaa"
OTHER_LIST_ID = "cccccccccc"
INVISIBLE_PAD = "\u034f\u200c" * 20


def campaign_html(body: str, view_link: str) -> str:
    """The shape of a current-builder archive_html, kept small for tests."""
    return (
        "<html><head><title>*|MC:SUBJECT|*</title><style>p{margin:0}</style></head><body>"
        f'<span style="display: none;">Preview text{INVISIBLE_PAD}</span>'
        '<table><tr><td class="mceTextBlockContainer"><div class="mceText">'
        f'<p><a href="{view_link}">View this email in your browser</a></p></div></td></tr>'
        f'<tr><td class="mceText"><p>{body}</p></td></tr>'
        '<tr><td class="mceFooterSection"><p><em>Copyright (C) 2026 Example Workspace. '
        "All rights reserved.</em></p><p>Our mailing address is:</p><p>Want to change how "
        'you receive these emails? <a href="https://x.list-manage.com/unsubscribe?e=[UNIQID]">'
        "unsubscribe</a></p></td></tr></table></body></html>"
    )


def plain_text(body: str) -> str:
    return (
        "View this email in your browser (*|ARCHIVE|*)\nLogo\n\n"
        f"{body}\n\nLogo\n\nCopyright (C) *|CURRENT_YEAR|* *|LIST:COMPANY|*. All rights reserved.\n"
        "Our mailing address is:\n*|LIST_ADDRESS|*\n"
        "You can update your preferences (*|UPDATE_PROFILE|*) or unsubscribe (*|UNSUB|*)\n"
    )


def _error(status: int, title: str, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"title": title, "status": status, "detail": detail})


class FakeMailchimp:
    api_key = API_KEY
    list_id = LIST_ID

    def __init__(self):
        self.clock = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
        self.next_id = 0
        self.campaigns: dict[str, dict] = {}
        self.bodies: dict[str, str] = {}
        self.lists = {LIST_ID: "Example Workspace", OTHER_LIST_ID: "Partners"}
        self.members: dict[str, dict[str, dict]] = {LIST_ID: {}, OTHER_LIST_ID: {}}
        self.notes: dict[tuple[str, str], list[dict]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None
        self.vanish_on_content: set[str] = set()
        self.vanish_on_notes: set[str] = set()

    # -- data helpers -------------------------------------------------------
    def _tick(self) -> str:
        self.clock += timedelta(minutes=1)
        return self.clock.isoformat()

    def _id(self) -> str:
        self.next_id += 1
        return f"{self.next_id:010x}"

    def add_campaign(
        self, title: str, body: str, *, list_id: str | None = LIST_ID, folder_id: str = ""
    ) -> dict:
        cid = self._id()
        campaign = {
            "id": cid,
            "status": "save",
            "create_time": self._tick(),
            "send_time": "",
            "archive_url": f"http://eepurl.com/{cid}",
            "long_archive_url": f"https://us22.campaign-archive.com/?u=0&id={cid}",
            "recipients": {"list_id": list_id or "", "list_name": self.lists.get(list_id, "")},
            "settings": {
                "title": title,
                "subject_line": f"{title}: news",
                "preview_text": None,
                "folder_id": folder_id,
            },
        }
        self.campaigns[cid] = campaign
        self.bodies[cid] = body
        return campaign

    def edit_body(self, cid: str, body: str) -> None:
        self.bodies[cid] = body  # nothing on the campaign itself changes

    def send(self, cid: str, *, finished: bool = True) -> None:
        campaign = self.campaigns[cid]
        slug = campaign["settings"]["title"].lower().replace(" ", "-")
        campaign.update(
            status="sent" if finished else "sending",
            send_time=self._tick(),
            long_archive_url=f"https://mailchi.mp/{cid}/{slug}",
        )

    def rename(self, cid: str, title: str) -> None:
        self.campaigns[cid]["settings"]["title"] = title

    def delete_campaign(self, cid: str) -> None:
        self.campaigns.pop(cid)
        self.bodies.pop(cid)

    def add_member(self, name: str, *, list_id: str = LIST_ID) -> str:
        mid = f"{self._id():0>32}"
        self.members[list_id][mid] = {
            "id": mid,
            "full_name": name,
            "email_address": f"{name.split()[0].lower()}@example.com",
            "last_changed": "2026-10-10T18:39:02+00:00",
        }
        self.notes[(list_id, mid)] = []
        return mid

    def add_note(self, mid: str, text: str, *, list_id: str = LIST_ID) -> dict:
        ts = self._tick()
        note = {
            "id": int(self._id(), 16),
            "note": text + "\n",
            "created_at": ts,
            "updated_at": ts,
            "created_by": "Anand",
        }
        self.notes[(list_id, mid)].append(note)
        return note

    def edit_note(self, mid: str, note_id: int, text: str, *, list_id: str = LIST_ID) -> None:
        for note in self.notes[(list_id, mid)]:
            if note["id"] == note_id:
                note.update(note=text + "\n", updated_at=self._tick())

    def delete_note(self, mid: str, note_id: int, *, list_id: str = LIST_ID) -> None:
        self.notes[(list_id, mid)] = [n for n in self.notes[(list_id, mid)] if n["id"] != note_id]

    def _member_view(self, list_id: str, member: dict, fields: str) -> dict:
        view = dict(member)
        if "members.email_address" not in fields:  # live: only the fields asked for
            view.pop("email_address", None)
        notes = self.notes.get((list_id, member["id"])) or []
        if notes:  # live: last_note exists only while the contact has notes
            last = notes[-1]
            view["last_note"] = {"note_id": last["id"], "note": last["note"].strip()}
        return view

    # -- HTTP ---------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("authorization", "")
        password = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)[1] if auth else ""
        if request.url.host != "us22.api.mailchimp.com":
            return _error(
                403, "Forbidden", "The API key provided is linked to a different datacenter."
            )
        if password != API_KEY:
            return _error(401, "API Key Invalid", "Your API key may be invalid.")
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, text="unavailable")

        q = request.url.params
        parts = [p for p in request.url.path.removeprefix("/3.0").split("/") if p]
        offset, count = int(q.get("offset", 0)), int(q.get("count", 10))

        def page(key: str, items: list[dict]) -> httpx.Response:
            return httpx.Response(
                200, json={key: items[offset : offset + count], "total_items": len(items)}
            )

        if parts == []:
            return httpx.Response(200, json={"account_id": "acct1", "account_name": "Example"})
        if parts == ["campaigns"]:
            items = sorted(self.campaigns.values(), key=lambda c: c["create_time"])
            if q.get("sort_dir") == "DESC":
                items.reverse()
            return page("campaigns", items)
        if len(parts) == 3 and parts[0] == "campaigns" and parts[2] == "content":
            cid = parts[1]
            if cid not in self.campaigns or cid in self.vanish_on_content:
                return _error(
                    404, "Resource Not Found", "The requested resource could not be found."
                )
            campaign = self.campaigns[cid]
            if campaign["status"] in ("sent", "sending"):
                link = f"{campaign['long_archive_url']}?e=[UNIQID]"
            else:
                link = f"https://us22.campaign-archive.com/?e=[UNIQID]&u=0&id={cid}"
            body = {"archive_html": campaign_html(self.bodies[cid], link)}
            if campaign["status"] in ("sent", "sending"):
                body["plain_text"] = plain_text(self.bodies[cid])
            return httpx.Response(200, json=body)
        if parts == ["lists"]:
            return page("lists", [{"id": k, "name": v} for k, v in self.lists.items()])
        if len(parts) == 3 and parts[0] == "lists" and parts[2] == "members":
            members = self.members.get(parts[1], {})
            fields = q.get("fields", "")
            return page(
                "members", [self._member_view(parts[1], m, fields) for m in members.values()]
            )
        if len(parts) == 5 and parts[2] == "members" and parts[4] == "notes":
            key = (parts[1], parts[3])
            if key not in self.notes or parts[3] in self.vanish_on_notes:
                return _error(
                    404, "Resource Not Found", "The requested resource could not be found."
                )
            return page("notes", sorted(self.notes[key], key=lambda n: n["created_at"]))
        return _error(404, "Resource Not Found", "The requested resource could not be found.")

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake() -> FakeMailchimp:
    return FakeMailchimp()


@pytest.fixture
def seeded(fake: FakeMailchimp) -> FakeMailchimp:
    """A sent campaign, a draft and one contact with a note, as on the live account."""
    launch = fake.add_campaign("Spring launch", "The new checkout flow ships on 15 March.")
    fake.send(launch["id"])
    fake.add_campaign("Webinar", "Join our product webinar on 20 March.")
    member = fake.add_member("Anand Prakash Srivastava")
    fake.add_note(member, "Prefers updates about the checkout redesign.")
    return fake


@pytest.fixture
def campaign_named(fake: FakeMailchimp):
    """Look up a campaign by its title."""

    def lookup(title: str) -> dict:
        return next(c for c in fake.campaigns.values() if c["settings"]["title"] == title)

    return lookup
