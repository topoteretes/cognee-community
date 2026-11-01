"""An in-memory Klaviyo account that answers like the REST API (revision 2026-10-15).

Shaped after Klaviyo's OpenAPI spec: JSON:API documents with ``data``,
``included`` and a cursor ``links.next``; ``filter=greater-or-equal(...)`` and
``sort`` on the update time; sparse fieldsets (``fields[type]``, nested names
included); ``include``; 404 for a record that does not exist. Passed to the
connector as its ``requests``-like session.

Every change moves only the update time of what it touches, like the API does
for the record it saves: editing a campaign's variation or template leaves the
campaign's ``updated_at`` alone, which is the case the connector must handle.

As on a real account (checked live against revision 2026-10-15), a flow email's template is a
copy that ``/templates`` does not list (``listed=False``) but answers by id, and
editing it leaves the flow's ``updated`` alone.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

API = "https://a.klaviyo.com/api"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def stamp(minutes: float) -> str:
    """Klaviyo's update-time format, ``minutes`` after T0."""
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class Resp:
    def __init__(self, status: int, body: Any = None, headers: dict | None = None, text=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._text = text

    def json(self) -> Any:
        if self._text is not None:
            return json.loads(self._text)
        return self._body


def error(status: int, code: str = "error", detail: str = "") -> Resp:
    return Resp(
        status,
        {"errors": [{"id": "e1", "status": status, "code": code, "title": "t", "detail": detail}]},
    )


_FILTER = re.compile(r"^greater-or-equal\((\w+),([^)]+)\)$")
_PAGE_SIZES = {"campaigns": 100, "flows": 50, "templates": 10}


class FakeKlaviyo:
    def __init__(self, account_id: str = "ACCOUNT1"):
        self.account_id = account_id
        self.clock = 0.0  # minutes after T0; every change moves it on
        self.campaigns: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.variations: dict[str, dict] = {}
        self.flows: dict[str, dict] = {}
        self.templates: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []  # (path, params) of every request
        self.headers_seen: list[dict] = []
        # path regex -> list of responses served (and consumed) before the real answer
        self.failures: list[tuple[str, Resp]] = []
        self.page_size: dict[str, int] = dict(_PAGE_SIZES)
        self.next_link_host = "a.klaviyo.com"
        self.flow_definitions = True  # False: flows answer without their definition
        self.closed = False

    # -- building the account -------------------------------------------------
    def tick(self) -> str:
        self.clock += 5
        return stamp(self.clock)

    def add_template(
        self, tid: str, name: str, html: str = "", text: str | None = None, *, listed: bool = True
    ) -> None:
        self.templates[tid] = {
            "name": name,
            "editor_type": "CODE",
            "html": html,
            "text": text,
            "listed": listed,
            "updated": self.tick(),
        }

    def edit_template(self, tid: str, **fields) -> None:
        self.templates[tid].update(fields, updated=self.tick())

    def add_campaign(
        self,
        cid: str,
        name: str,
        *,
        status: str = "sent",
        subject: str = "",
        preview: str = "",
        template_id: str | None = None,
        channel: str = "email",
        body: str = "",
        description: str | None = None,
        archived: bool = False,
    ) -> None:
        mid, vid = f"{cid}-m1", f"{cid}-v1"
        details: dict[str, Any] = {"channel": channel}
        if channel == "email":
            details.update(
                subject=subject,
                preview_text=preview,
                from_email="hello@brand.test",
                from_label="Brand",
                reply_to_email="replies@brand.test",
                template_id=template_id,
            )
        else:
            details.update(body=body)
        self.variations[vid] = {"name": "Variation A", "details": details, "message": mid}
        self.messages[mid] = {
            "name": f"{name} message",
            "status": status,
            "send_time": "2026-10-02T15:00:00+00:00",
            "campaign": cid,
            "variations": [vid],
            "updated": self.tick(),
        }
        self.campaigns[cid] = {
            "name": name,
            "description": description,
            "archived": archived,
            "messages": [mid],
            "updated_at": self.tick(),
        }

    def edit_campaign(self, cid: str, **fields) -> None:
        self.campaigns[cid].update(fields, updated_at=self.tick())

    def edit_variation(self, cid: str, **details) -> None:
        """Edit the copy of a campaign's first variation; the campaign's updated_at stays."""
        vid = self.messages[self.campaigns[cid]["messages"][0]]["variations"][0]
        self.variations[vid]["details"].update(details)
        self.tick()

    def set_status(self, cid: str, status: str) -> None:
        self.messages[self.campaigns[cid]["messages"][0]]["status"] = status
        self.tick()

    def delete_campaign(self, cid: str) -> None:
        for mid in self.campaigns.pop(cid)["messages"]:
            for vid in self.messages.pop(mid)["variations"]:
                self.variations.pop(vid, None)

    def add_flow(
        self,
        fid: str,
        name: str,
        actions: list[dict],
        *,
        status: str = "live",
        trigger_type: str = "Added to List",
        archived: bool = False,
    ) -> None:
        self.flows[fid] = {
            "name": name,
            "status": status,
            "archived": archived,
            "trigger_type": trigger_type,
            "definition": {
                "triggers": [{"type": "list", "id": "L1"}],
                "actions": actions,
                "entry_action_id": actions[0]["id"] if actions else None,
            },
            "updated": self.tick(),
        }

    def edit_flow(self, fid: str, **fields) -> None:
        self.flows[fid].update(fields, updated=self.tick())

    # -- HTTP -----------------------------------------------------------------
    def close(self) -> None:
        self.closed = True

    def get(self, url: str, params: Any = None, headers: dict | None = None, timeout=None):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query.update(dict(params or []))
        path = parts.path.removeprefix("/api")
        self.calls.append((path, query))
        self.headers_seen.append(dict(headers or {}))
        for index, (pattern, response) in enumerate(self.failures):
            if re.fullmatch(pattern, path):
                del self.failures[index]
                return response
        return self._route(path, query)

    def _route(self, path: str, query: dict) -> Resp:
        segments = path.strip("/").split("/")
        if segments == ["accounts"]:
            return Resp(200, {"data": [{"type": "account", "id": self.account_id}]})
        if len(segments) == 1 and segments[0] in ("campaigns", "flows", "templates"):
            return self._list(segments[0], query)
        if len(segments) == 2:
            collection, rid = segments
            if collection == "campaigns" and rid in self.campaigns:
                return self._one(self._campaign(rid, query), query, self._campaign_includes(rid))
            if collection == "campaign-messages" and rid in self.messages:
                return self._one(self._message(rid, query), query, self._variations(rid, query))
            if collection == "flows" and rid in self.flows:
                return self._one(self._flow(rid, query), query, [])
            if collection == "templates" and rid in self.templates:
                return self._one(self._template(rid, query), query, [])
        if segments[::2] == ["flows", "flow-actions"] and segments[1] in self.flows:
            actions = self.flows[segments[1]]["definition"]["actions"]
            data = [
                {"type": "flow-action", "id": a["id"], "attributes": {"definition": a}}
                for a in actions
            ]
            return Resp(200, {"data": data, "links": {"self": API}})
        return error(404, "not_found", f"{path} was not found")

    def _one(self, data: dict, query: dict, included: list[dict]) -> Resp:
        body: dict[str, Any] = {"data": data, "links": {"self": API}}
        if query.get("include"):
            body["included"] = included
        return Resp(200, body)

    def _list(self, collection: str, query: dict) -> Resp:
        records = {
            "campaigns": self.campaigns,
            "flows": self.flows,
            "templates": self.templates,
        }[collection]
        field = "updated_at" if collection == "campaigns" else "updated"
        ids = [i for i in records if records[i].get("listed", True)]
        match = _FILTER.match(query.get("filter", ""))
        if query.get("filter") and not match:
            return error(400, "invalid", "unsupported filter")
        if match:
            name, since = match.groups()
            assert name == field, f"filter on {name}, not {field}"
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            ids = [
                i
                for i in ids
                if datetime.fromisoformat(records[i][field].replace("Z", "+00:00")) >= since_dt
            ]
        sort = query.get("sort")
        if sort:
            key = sort.lstrip("-")
            assert key == field, f"sort on {key}, not {field}"
            ids.sort(key=lambda i: records[i][field], reverse=sort.startswith("-"))
        size = int(query.get("page[size]", self.page_size[collection]))
        assert size <= _PAGE_SIZES[collection], f"page[size] {size} over Klaviyo's maximum"
        size = min(size, self.page_size[collection])
        offset = int(query.get("page[cursor]", "0"))
        page = ids[offset : offset + size]
        build = {"campaigns": self._campaign, "flows": self._flow, "templates": self._template}
        body: dict[str, Any] = {
            "data": [build[collection](i, query) for i in page],
            "links": {"self": API},
        }
        if collection == "campaigns" and query.get("include"):
            body["included"] = [m for cid in page for m in self._campaign_includes(cid, query)]
        if offset + size < len(ids):
            rest = {k: v for k, v in query.items() if k != "page[cursor]"}
            rest["page[cursor]"] = str(offset + size)
            body["links"]["next"] = (
                f"https://{self.next_link_host}/api/{collection}?{urlencode(rest)}"
            )
        return Resp(200, body)

    # -- serializers ----------------------------------------------------------
    def _campaign(self, cid: str, query: dict) -> dict:
        c = self.campaigns[cid]
        attributes = {
            "created_at": stamp(0),
            "updated_at": c["updated_at"],
            "definition": {
                "name": c["name"],
                "description": c["description"],
                "archived": c["archived"],
                "builder": "wizard",
                "send_settings": {"send_timezone": "UTC"},
            },
        }
        return {
            "type": "campaign",
            "id": cid,
            "attributes": _sparse(attributes, query.get("fields[campaign]")),
            "relationships": {
                "campaign-messages": {
                    "data": [{"type": "campaign-message", "id": m} for m in c["messages"]]
                }
            },
        }

    def _campaign_includes(self, cid: str, query: dict | None = None) -> list[dict]:
        return [self._message(m, query or {}) for m in self.campaigns[cid]["messages"]]

    def _message(self, mid: str, query: dict) -> dict:
        m = self.messages[mid]
        attributes = {
            "created": stamp(0),
            "updated": m["updated"],
            "definition": {
                "name": m["name"],
                "status": m["status"],
                "send_options": {"send_time": m["send_time"], "send_strategy": "static"},
                "tracking_options": {"is_tracking_opens": True},
            },
        }
        return {
            "type": "campaign-message",
            "id": mid,
            "attributes": _sparse(attributes, query.get("fields[campaign-message]")),
            "relationships": {
                "campaign-variations": {
                    "data": [{"type": "campaign-variation", "id": v} for v in m["variations"]]
                }
            },
        }

    def _variations(self, mid: str, query: dict) -> list[dict]:
        return [
            {
                "type": "campaign-variation",
                "id": vid,
                "attributes": {
                    "definition": {
                        "name": self.variations[vid]["name"],
                        "details": dict(self.variations[vid]["details"]),
                    }
                },
            }
            for vid in self.messages[mid]["variations"]
        ]

    def _flow(self, fid: str, query: dict) -> dict:
        f = self.flows[fid]
        attributes = {
            "name": f["name"],
            "status": f["status"],
            "archived": f["archived"],
            "trigger_type": f["trigger_type"],
            "created": stamp(0),
            "updated": f["updated"],
        }
        attributes = _sparse(attributes, query.get("fields[flow]"))
        if query.get("additional-fields[flow]") == "definition" and self.flow_definitions:
            attributes["definition"] = json.loads(json.dumps(f["definition"]))
        return {"type": "flow", "id": fid, "attributes": attributes}

    def _template(self, tid: str, query: dict) -> dict:
        t = self.templates[tid]
        attributes = {
            "name": t["name"],
            "editor_type": t["editor_type"],
            "html": t["html"],
            "text": t["text"],
            "created": stamp(0),
            "updated": t["updated"],
        }
        return {
            "type": "template",
            "id": tid,
            "attributes": _sparse(attributes, query.get("fields[template]")),
        }


def _sparse(attributes: dict, fields: str | None) -> dict:
    """Keep only the listed attributes; ``definition.archived`` keeps that nested key."""
    if not fields:
        return attributes
    kept: dict[str, Any] = {}
    for name in fields.split(","):
        head, _, rest = name.partition(".")
        if head not in attributes:
            continue
        if not rest:
            kept[head] = attributes[head]
        elif isinstance(attributes[head], dict):
            inner = _sparse(attributes[head], rest)
            kept.setdefault(head, {}).update(inner)
    return kept


# -- flow actions, shaped like the 2026-10-15 flow definition --------------------
def email_step(aid: str, name: str, subject: str, template_id: str, nxt: str | None = None):
    return {
        "id": aid,
        "type": "send-email",
        "links": {"next": nxt},
        "data": {
            "status": "live",
            "message": {
                "name": name,
                "subject_line": subject,
                "preview_text": f"{subject} preview",
                "from_email": "hello@brand.test",
                "from_label": "Brand",
                "template_id": template_id,
            },
        },
    }


def delay_step(aid: str, value: int, unit: str, nxt: str | None = None):
    return {
        "id": aid,
        "type": "time-delay",
        "links": {"next": nxt},
        "data": {"value": value, "unit": unit},
    }


def sms_step(aid: str, body: str, nxt: str | None = None):
    return {
        "id": aid,
        "type": "send-sms",
        "links": {"next": nxt},
        "data": {"status": "live", "message": {"name": "SMS", "body": body}},
    }
