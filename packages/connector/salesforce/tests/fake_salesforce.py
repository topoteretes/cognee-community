"""An in-memory Salesforce org that answers the connector's requests like the real one.

Every shape here was recorded from a live Developer Edition org on 2026-10-29/30
(issues/salesforce-4752/observations in the build workspace); the comments say which. It
understands only the SOQL the connector writes - SELECT ... FROM ... WHERE ... ORDER BY -
not SOQL in general.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import httpx

from cognee_community_connector_salesforce import SalesforceClient
from cognee_community_connector_salesforce.salesforce import _iter_rows

LOGIN = "https://acme-dev-ed.my.salesforce.com"
INSTANCE = "https://acme-dev-ed.my.salesforce.com"
ORG_ID = "00Dg500000FAKEORGA"
USER_ID = "005g500000FAKEUSRA"
VERSION = "66.0"
_PREFIX = {
    "Account": "001",
    "Contact": "003",
    "Opportunity": "006",
    "Case": "500",
    "CaseComment": "00a",
    "Task": "00T",
    "FeedItem": "0D5",
    "FeedComment": "0D7",
}
_NOT_FOUND = [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]


def stamp(moment: datetime) -> str:
    # recorded format: 2026-10-29T17:32:28.000+0000 (no colon in the offset)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


class FakeSalesforce:
    def __init__(self, *, org_id: str = ORG_ID, login: str = LOGIN):
        self.org_id = org_id
        self.login = login
        self.instance = login
        self.client_id, self.client_secret = "fake-key", "fake-secret"
        self.clock = datetime(2026, 10, 29, 17, 32, 28, tzinfo=timezone.utc)
        self.counter = 0
        # {object: {id: record dict}} - parents, with IsDeleted for the Recycle Bin
        self.records: dict[str, dict[str, dict]] = {
            o: {} for o in ("Account", "Contact", "Opportunity", "Case")
        }
        self.comments: dict[str, dict] = {}  # CaseComment id -> row
        self.tasks: dict[str, dict] = {}
        self.posts: dict[str, dict] = {}  # FeedItem id -> row (with replies list)
        self.token_serial = 0
        self.valid_token: str | None = None
        self.calls: list[tuple[str, str, dict]] = []
        self.page_size = 2000
        self.locators: dict[str, list[dict]] = {}
        # getDeleted: the org's replication start, and how far latestDateCovered lags
        self.replication_enabled = self.clock
        self.covered_lag = timedelta(0)
        self.earliest_available: datetime | None = None
        self.api_used, self.api_limit = 38, 15000
        self.queued: list[httpx.Response] = []
        self.activities_access = True
        # recorded (describeGlobal, 2026-10-30): feed tracking on for all four objects
        self.feed_enabled = {"Account", "Contact", "Opportunity", "Case"}
        self.tie_flips: dict[str, bool] = {}

    # -- fixtures ---------------------------------------------------------------
    def tick(self, seconds: int = 1) -> datetime:
        self.clock += timedelta(seconds=seconds)
        return self.clock

    def new_id(self, sobject: str) -> str:
        self.counter += 1
        return f"{_PREFIX[sobject]}g5000000{self.counter:04d}AAA"

    def add(self, sobject: str, *, same_second: bool = False, **fields) -> str:
        record_id = self.new_id(sobject)
        when = self.clock if same_second else self.tick()
        self.records[sobject][record_id] = {
            "Id": record_id,
            "SystemModstamp": when,
            "CreatedDate": when,
            "IsDeleted": False,
            "Owner": "Anmol Gupta",
            **fields,
        }
        return record_id

    def edit(self, sobject: str, record_id: str, **fields) -> None:
        record = self.records[sobject][record_id]
        record.update(fields)
        record["SystemModstamp"] = self.tick()

    def delete(self, sobject: str, record_id: str) -> None:
        record = self.records[sobject][record_id]
        record["IsDeleted"] = True
        record["SystemModstamp"] = record["DeletedDate"] = self.tick()

    def undelete(self, sobject: str, record_id: str) -> None:
        # recorded: an undeleted record comes back with a NEW, later SystemModstamp and
        # drops out of getDeleted
        record = self.records[sobject][record_id]
        record["IsDeleted"] = False
        record.pop("DeletedDate", None)
        record["SystemModstamp"] = self.tick()

    def purge(self, sobject: str, record_id: str) -> None:
        """Gone for good (emptied Recycle Bin): no longer in queryAll nor getDeleted, and
        getDeleted no longer vouches for anything before now."""
        self.records[sobject].pop(record_id)
        self.earliest_available = self.clock

    def purge_comment(self, comment_id: str) -> None:
        """A deleted comment emptied from the Recycle Bin: queryAll no longer has it."""
        self.comments.pop(comment_id)

    def add_comment(
        self, case_id: str, body: str, *, published: bool = True, same_second: bool = False
    ) -> str:
        # recorded: a new comment does NOT move its Case's SystemModstamp
        comment_id = self.new_id("CaseComment")
        when = self.clock if same_second else self.tick()
        self.comments[comment_id] = {
            "Id": comment_id,
            "ParentId": case_id,
            "CommentBody": body,
            "IsPublished": published,
            "CreatedDate": when,
            "SystemModstamp": when,
            "IsDeleted": False,
        }
        return comment_id

    def delete_comment(self, comment_id: str) -> None:
        row = self.comments[comment_id]
        row["IsDeleted"] = True
        row["SystemModstamp"] = self.tick()

    def add_task(
        self,
        subject: str,
        *,
        what_id: str | None = None,
        who_id: str | None = None,
        description: str = "",
        status: str = "Completed",
    ) -> str:
        task_id = self.new_id("Task")
        when = self.tick()
        self.tasks[task_id] = {
            "Id": task_id,
            "WhatId": what_id,
            "WhoId": who_id,
            "Subject": subject,
            "Description": description,
            "Status": status,
            "ActivityDate": None,
            "CreatedDate": when,
            "SystemModstamp": when,
            "IsDeleted": False,
        }
        return task_id

    def add_post(
        self, parent_id: str, body: str, *, kind: str = "TextPost", rich: bool = False
    ) -> str:
        post_id = self.new_id("FeedItem")
        when = self.tick()
        self.posts[post_id] = {
            "Id": post_id,
            "ParentId": parent_id,
            "Type": kind,
            "Body": body,
            # recorded: a post made through the API is plain text (IsRichText false)
            "IsRichText": rich,
            "CreatedDate": when,
            "SystemModstamp": when,
            "IsDeleted": False,
            "replies": [],
        }
        return post_id

    def add_reply(self, post_id: str, body: str, *, rich: bool = False) -> None:
        # recorded: a late reply DOES move its post's SystemModstamp
        when = self.tick()
        post = self.posts[post_id]
        post["replies"].append(
            {
                "Id": self.new_id("FeedComment"),
                "CommentBody": body,
                "IsRichText": rich,
                "CreatedDate": when,
            }
        )
        post["SystemModstamp"] = when

    def mirror_comment_in_feed(self, case_id: str) -> None:
        # recorded: every case comment also appears in the feed as a CaseCommentPost
        # with an EMPTY body
        self.add_post(case_id, "", kind="CaseCommentPost")

    def expire_token(self) -> None:
        self.valid_token = None

    # -- transport --------------------------------------------------------------
    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(lambda request: self._dated(self._handle(request)))
        )

    def _dated(self, response: httpx.Response) -> httpx.Response:
        # every Salesforce response carries the org's clock in Date, the token one included
        if "date" not in response.headers:
            response.headers["date"] = format_datetime(self.clock, usegmt=True)
        return response

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((request.method, url.path, params))
        if self.queued:
            return self.queued.pop(0)
        if url.path == "/services/oauth2/token":
            return self._token(request)
        if (
            request.headers.get("authorization") != f"Bearer {self.valid_token}"
            or not self.valid_token
        ):
            return httpx.Response(
                401,
                headers={"www-authenticate": "Token"},
                json=[{"message": "Session expired or invalid", "errorCode": "INVALID_SESSION_ID"}],
            )
        self.api_used += 1
        if self.api_used > self.api_limit:
            # Salesforce's documented answer to an exhausted daily allowance (not recorded:
            # reaching 15,000 calls on the test org wasn't worth it)
            return self._json(
                403,
                [
                    {
                        "message": "TotalRequests Limit exceeded.",
                        "errorCode": "REQUEST_LIMIT_EXCEEDED",
                    }
                ],
            )
        base = f"/services/data/v{VERSION}"
        if url.path in (f"{base}/query", f"{base}/queryAll"):
            return self._query(params["q"], include_deleted=url.path.endswith("All"))
        if url.path == f"{base}/sobjects/":
            return self._describe_global()
        if url.path.startswith(f"{base}/query/"):
            return self._more(url.path.rsplit("/", 1)[-1])
        match = re.fullmatch(rf"{re.escape(base)}/sobjects/(\w+)/deleted/", url.path)
        if match:
            return self._deleted(match.group(1), params)
        match = re.fullmatch(rf"{re.escape(base)}/sobjects/(\w+)/(\w+)", url.path)
        if match:
            return self._record(match.group(1), match.group(2))
        return self._json(
            404, [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
        )

    def _json(self, status: int, body, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(
            status,
            json=body,
            headers={
                "sforce-limit-info": f"api-usage={self.api_used}/{self.api_limit}",
                # the org's own clock, as every Salesforce response carries it
                "date": format_datetime(self.clock, usegmt=True),
                **(headers or {}),
            },
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form.get("grant_type") == "client_credentials"
        if (
            form.get("client_id") != self.client_id
            or form.get("client_secret") != self.client_secret
        ):
            # recorded: OAuth's own error shape, not the usual error list
            return httpx.Response(
                400,
                json={"error": "invalid_client", "error_description": "invalid client credentials"},
            )
        self.token_serial += 1
        self.valid_token = f"00D!fake-token-{self.token_serial}"
        # recorded: no expires_in, no refresh_token; issued_at is epoch ms as a string
        return httpx.Response(
            200,
            json={
                "access_token": self.valid_token,
                "signature": "sig",
                "scope": "api",
                "instance_url": self.instance,
                "id": f"https://login.salesforce.com/id/{self.org_id}/{USER_ID}",
                "token_type": "Bearer",
                "issued_at": "1793305840088",
            },
        )

    # -- SOQL -------------------------------------------------------------------
    def _describe_global(self) -> httpx.Response:
        # recorded shape, cut to the fields the connector reads (358 objects on the org)
        names = ["Account", "AccountFeed", "Case", "CaseComment", "CaseFeed", "Contact"]
        names += ["ContactFeed", "Opportunity", "OpportunityFeed", "Task"]
        return self._json(
            200,
            {
                "encoding": "UTF-8",
                "maxBatchSize": 200,
                "sobjects": [
                    {"name": n, "feedEnabled": n in self.feed_enabled, "queryable": True}
                    for n in names
                ],
            },
        )

    def _query(self, soql: str, *, include_deleted: bool) -> httpx.Response:
        if not self.activities_access and re.search(r"\bFROM Task\b|\bFROM Tasks\b", soql):
            # recorded without "Access Activities": INVALID_TYPE
            return self._json(
                400,
                [{"message": "sObject type 'Task' is not supported.", "errorCode": "INVALID_TYPE"}],
            )
        select, source, where, _order = _split_soql(soql)
        untracked = source.endswith("Feed") and source.removesuffix("Feed") not in self.feed_enabled
        if untracked or (
            any(c.startswith("(") and " FROM Feeds " in c for c in select)
            and source not in self.feed_enabled
        ):
            # documented, not recorded (the test org has feed tracking on everywhere)
            name = source if untracked else "Feeds"
            message = (
                f"sObject type '{name}' is not supported."
                if untracked
                else f"Didn't understand relationship '{name}' in FROM part of query call."
            )
            return self._json(400, [{"message": message, "errorCode": "INVALID_TYPE"}])
        rows = self._source_rows(source, include_deleted)
        rows = [r for r in rows if _matches(r, where)]
        rows.sort(key=lambda r: (r.get("SystemModstamp"), r["Id"]))
        rendered = [self._render(source, r, select) for r in rows]
        return self._page(rendered)

    def _page(self, rows: list[dict], offset: int = 0) -> httpx.Response:
        chunk = rows[offset : offset + self.page_size]
        done = offset + self.page_size >= len(rows)
        body = {"totalSize": len(rows), "done": done, "records": chunk}
        if not done:
            # recorded: /services/data/v66.0/query/0r8xx62cCdXo6hwAIB-200 on a 220-row listing,
            # then the last 20 rows with done: true and no nextRecordsUrl key at all
            locator = f"0r8xx62cCdXoFAKE{len(self.locators):02d}-{offset + self.page_size}"
            self.locators[locator] = rows
            body["nextRecordsUrl"] = f"/services/data/v{VERSION}/query/{locator}"
        return self._json(200, body)

    def _more(self, locator: str) -> httpx.Response:
        if locator not in self.locators:
            # recorded for a malformed locator
            return self._json(
                400, [{"message": f"malformed id {locator}", "errorCode": "INVALID_QUERY_LOCATOR"}]
            )
        return self._page(self.locators[locator], int(locator.rsplit("-", 1)[1]))

    def _source_rows(self, source: str, include_deleted: bool) -> list[dict]:
        if source in self.records:
            rows = list(self.records[source].values())
        elif source == "CaseComment":
            rows = list(self.comments.values())
        elif source == "Task":
            rows = list(self.tasks.values())
        elif source.endswith("Feed"):
            parent = source[: -len("Feed")]
            rows = [p for p in self.posts.values() if self._object_of(p["ParentId"]) == parent]
        else:
            raise AssertionError(f"the fake does not know {source}")
        return [r for r in rows if include_deleted or not r.get("IsDeleted")]

    def _object_of(self, record_id: str | None) -> str | None:
        for sobject, prefix in _PREFIX.items():
            if record_id and record_id.startswith(prefix):
                return sobject
        return None

    def _render(self, source: str, row: dict, select: list[str]) -> dict:
        out = {
            "attributes": {
                "type": source,
                "url": f"/services/data/v{VERSION}/sobjects/{source}/{row['Id']}",
            }
        }
        for column in select:
            if column.startswith("("):
                sub_select, relation, sub_where, sub_order = _split_soql(column[1:-1])
                out[relation] = self._children(row, relation, sub_select, sub_where, sub_order)
            elif "." in column:
                relation, _field = column.split(".", 1)
                out[relation] = self._related(row, relation)
            elif column in ("SystemModstamp", "CreatedDate"):
                out[column] = stamp(row[column]) if row.get(column) else None
            else:
                out[column] = row.get(column)
        return out

    def _related(self, row: dict, relation: str) -> dict | None:
        if relation == "Owner":
            # recorded: Owner is polymorphic, attributes type "Name"
            return {"attributes": {"type": "Name"}, "Name": row.get("Owner")}
        if relation == "CreatedBy":
            return {"attributes": {"type": "User"}, "Name": "Anmol Gupta"}
        parent_id = row.get(f"{relation}Id")
        parent = self.records.get(relation, {}).get(parent_id) if parent_id else None
        if not parent or parent.get("IsDeleted"):
            return None
        return {"attributes": {"type": relation}, "Name": parent.get("Name")}

    def _children(
        self, row: dict, relation: str, select: list[str], where: str, order: str
    ) -> dict | None:
        if relation == "CaseComments":
            items = [
                c
                for c in self.comments.values()
                if c["ParentId"] == row["Id"] and not c["IsDeleted"]
            ]

            def render(c):
                return {
                    "CommentBody": c["CommentBody"],
                    "IsPublished": c["IsPublished"],
                    "CreatedDate": stamp(c["CreatedDate"]),
                    "CreatedBy": self._related(c, "CreatedBy"),
                }
        elif relation == "Tasks":
            # Account/Opportunity/Case tasks follow WhatId, a Contact's follow WhoId (recorded)
            key = "WhoId" if row["Id"].startswith(_PREFIX["Contact"]) else "WhatId"
            items = [t for t in self.tasks.values() if t[key] == row["Id"] and not t["IsDeleted"]]

            def render(t):
                return {
                    "Subject": t["Subject"],
                    "Description": t["Description"],
                    "Status": t["Status"],
                    "ActivityDate": t["ActivityDate"],
                    "CreatedDate": stamp(t["CreatedDate"]),
                }
        elif relation == "Feeds":
            kinds = re.search(r"Type IN \(([^)]*)\)", where)
            allowed = {k.strip(" '") for k in kinds.group(1).split(",")} if kinds else None
            items = [
                p
                for p in self.posts.values()
                if p["ParentId"] == row["Id"]
                and not p["IsDeleted"]
                and (allowed is None or p["Type"] in allowed)
            ]
            replies_column = next(c for c in select if c.startswith("("))
            reply_limit = _limit(_split_soql(replies_column[1:-1])[3])

            def render(p):
                replies = [
                    {
                        "CommentBody": r["CommentBody"],
                        "IsRichText": r["IsRichText"],
                        "CreatedDate": stamp(r["CreatedDate"]),
                        "CreatedBy": self._related(r, "CreatedBy"),
                    }
                    for r in sorted(p["replies"], key=lambda r: (r["CreatedDate"], r["Id"]))
                ]
                return {
                    "Type": p["Type"],
                    "Body": p["Body"],
                    "IsRichText": p["IsRichText"],
                    "CreatedDate": stamp(p["CreatedDate"]),
                    "CreatedBy": self._related(p, "CreatedBy"),
                    "FeedComments": _sub_result(replies[:reply_limit]),
                }
        else:
            raise AssertionError(f"the fake does not know subquery {relation}")
        # Ties on CreatedDate are real (two comments on one live case share one, recorded
        # 2026-10-30) and Salesforce promises no order among them, so unless the query
        # breaks ties on Id, the fake hands them out in the opposite order on every read.
        if "Id" in order.split("LIMIT")[0]:
            items.sort(key=lambda i: (i["CreatedDate"], i["Id"]), reverse="DESC" in order)
        else:
            self.tie_flips[relation] = flip = not self.tie_flips.get(relation, False)
            items.sort(key=lambda i: i["Id"], reverse=flip)
            items.sort(key=lambda i: i["CreatedDate"], reverse="DESC" in order)
        return _sub_result([render(i) for i in items][: _limit(order)])

    def _deleted(self, sobject: str, params: dict) -> httpx.Response:
        start, end = _parse(params["start"]), _parse(params["end"])
        if start < self.clock - timedelta(days=30):
            return self._json(
                400,
                [
                    {
                        "message": "startDate cannot be more than 30 days ago",
                        "errorCode": "INVALID_REPLICATION_DATE",
                    }
                ],
            )
        if start < self.replication_enabled:
            return self._json(
                400,
                [
                    {
                        "message": "startDate before org replication enabled date",
                        "errorCode": "INVALID_REPLICATION_DATE",
                    }
                ],
            )
        if start.replace(second=0) >= end.replace(second=0):
            # recorded on a run seconds after the last (2026-10-30); a 40-second window
            # across a minute boundary was answered, so the fake compares minutes
            return self._json(
                400,
                [
                    {
                        "message": "startDate must be at least one minute greater than endDate",
                        "errorCode": "INVALID_REPLICATION_DATE",
                    }
                ],
            )
        deleted = [
            {"deletedDate": stamp(r["DeletedDate"]), "id": r["Id"]}
            for r in self.records[sobject].values()
            if r.get("IsDeleted") and start <= r["DeletedDate"] <= end
        ]
        earliest = self.earliest_available or self.replication_enabled
        covered = end.replace(second=0) - self.covered_lag
        return self._json(
            200,
            {
                "deletedRecords": deleted,
                "earliestDateAvailable": stamp(earliest),
                "latestDateCovered": stamp(covered),
            },
        )

    def _record(self, sobject: str, record_id: str) -> httpx.Response:
        record = self.records.get(sobject, {}).get(record_id)
        if not record or record.get("IsDeleted"):
            # recorded: byte-identical for a deleted record and an id that never existed
            return self._json(404, _NOT_FOUND)
        return self._json(200, {"attributes": {"type": sobject}, "Id": record_id})


def _split_soql(soql: str) -> tuple[list[str], str, str, str]:
    """``(select columns, FROM object, WHERE text, ORDER BY text)`` of a top-level query."""
    depth, i, from_at = 0, 0, -1
    while i < len(soql):
        if soql[i] == "(":
            depth += 1
        elif soql[i] == ")":
            depth -= 1
        elif depth == 0 and soql.startswith(" FROM ", i):
            from_at = i
            break
        i += 1
    select = _columns(soql[len("SELECT ") : from_at])
    rest = soql[from_at + len(" FROM ") :]
    source, _, tail = rest.partition(" ")
    where, _, order = tail.partition("ORDER BY")
    return select, source, where.removeprefix("WHERE ").strip(), order.strip()


def _columns(text: str) -> list[str]:
    columns, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            columns.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        columns.append(current.strip())
    return columns


def _limit(order: str) -> int:
    found = re.search(r"LIMIT (\d+)", order)
    return int(found.group(1)) if found else 10_000


def _sub_result(items: list[dict]) -> dict | None:
    # recorded: a record with no children returns null for the subquery
    if not items:
        return None
    return {"totalSize": len(items), "done": True, "records": items}


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _matches(row: dict, where: str) -> bool:
    """The conditions the connector writes, joined by AND."""
    if not where:
        return True
    for part in _and_parts(where):
        part = part.strip()
        if part.startswith("(") and part.endswith(")"):
            if not _matches(row, part[1:-1]):
                return False
            continue
        if m := re.fullmatch(r"SystemModstamp (>=|>) (\S+)", part):
            op, bound = m.group(1), _parse(m.group(2))
            moment = row.get("SystemModstamp")
            if not moment or moment < bound or (op == ">" and moment == bound):
                return False
        elif m := re.fullmatch(r"(\w+) IN \((.*)\)", part):
            if row.get(m.group(1)) not in {i.strip(" '") for i in m.group(2).split(",")}:
                return False
        elif m := re.fullmatch(r"(\w+) (=|!=) (.+)", part):
            field, op, raw = m.groups()
            value = {"true": True, "false": False, "null": None}.get(raw, raw.strip("'"))
            if (row.get(field) == value) != (op == "="):
                return False
        else:
            raise AssertionError(f"the fake does not understand the condition {part!r}")
    return True


def _and_parts(where: str) -> list[str]:
    parts, depth, current, i = [], 0, "", 0
    while i < len(where):
        char = where[i]
        depth += char == "("
        depth -= char == ")"
        if depth == 0 and where.startswith(" AND ", i):
            parts.append(current)
            current, i = "", i + 5
            continue
        current += char
        i += 1
    parts.append(current)
    return parts


def client_for(fake: FakeSalesforce, sleeps: list | None = None) -> SalesforceClient:
    sleep = sleeps.append if sleeps is not None else (lambda _s: None)
    return SalesforceClient(
        fake.login, fake.client_id, fake.client_secret, http=fake.client(), sleep=sleep
    )


def sync(fake: FakeSalesforce, state: dict, *, client: SalesforceClient | None = None, **kwargs):
    """One run: ``(upserts by id, deleted ids, stats)``."""
    stats: dict = {}
    rows = list(_iter_rows(client or client_for(fake), state, stats, **kwargs))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = {row["id"] for row in rows if row["_deleted"]}
    return upserts, deletes, stats


def json_body(response: httpx.Response):
    return json.loads(response.content)


def hide_from_listing(fake: FakeSalesforce, sobject: str, record_id: str) -> None:
    """Leave one record out of every query of its object, while a GET still serves it."""
    original = fake._source_rows

    def without(source, include_deleted):
        rows = original(source, include_deleted)
        return [r for r in rows if r["Id"] != record_id] if source == sobject else rows

    fake._source_rows = without
