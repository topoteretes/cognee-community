"""Salesforce connector for cognee: a dlt resource that turns CRM records into memory.

Each Account, Contact, Opportunity and Case becomes one document carrying its own fields
*and* the text people write around it - case comments, tasks and Chatter posts with their
replies - and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        salesforce_source(
            login_url="https://acme.my.salesforce.com",
            client_id="...",
            client_secret="...",
        ),
        dataset_name="salesforce",
        primary_key="id",
        write_disposition="merge",  # required: the add pipeline defaults to "replace"
        self_improvement=False,  # no enrichment pass on every scheduled sync
    )
"""

import hashlib
import html
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("salesforce_connector")

# a fixed version, not "latest": a moving version is a moving response shape (v66.0
# answered every query here on a live org, 2026-10-29)
DEFAULT_API_VERSION = "66.0"
_API_VERSION = re.compile(r"^\d{2,3}\.0$")
SUPPORTED_OBJECTS = ("Account", "Contact", "Opportunity", "Case")
# record id prefixes; how a task's polymorphic WhatId / WhoId finds its parent's type
_KEY_PREFIX = {"001": "Account", "003": "Contact", "006": "Opportunity", "500": "Case"}
# requests per run: about 13% of a Developer Edition org's 15,000 a day (recorded)
DEFAULT_MAX_REQUESTS = 2000
# share of the org's daily allowance a run leaves for the other integrations using it
_ALLOWANCE_RESERVE = 0.05
# ids per "IN (...)": about 4,400 characters, far inside SOQL's 100,000 limit
_ID_CHUNK = 200
# re-read window behind each cursor ("SystemModstamp >=", never ">"): bulk-created
# records share a stamp (recorded), and a late commit can carry an older one
_CURSOR_OVERLAP = timedelta(minutes=5)
_RETRIES = 3  # attempts per request on 5xx or network errors, backing off 1s then 2s
# backoff one run may spend, so retries can't hold cognee's dlt staging lock for long
_MAX_TOTAL_WAIT = 60.0
_TIMEOUT = 30.0  # seconds
# Postgres, the shortest identifier limit among cognee's destinations
_MAX_RESOURCE_NAME = 63
# child rows per document, newest first, and replies per post: a document says what the
# record is about, not its full history
_MAX_CHILDREN = 20
_MAX_REPLIES = 10
# long text fields hold up to 32,000 characters; one pasted email would be the document
_MAX_TEXT = 3000
_MAX_SHORT = 255  # names and subjects (Salesforce caps Subject at 255)
# Salesforce stamps look like 2026-10-29T17:32:28.000+0000; Python 3.10's fromisoformat
# refuses the colon-less offset
_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")
_FRACTION = re.compile(r"\.(\d+)")
# getDeleted refuses a start over 30 days back (recorded); older cursors compare ids
_DELETED_HORIZON = timedelta(days=29)
# and a window under a minute (seen in a run log); a shorter one waits for the next run
_MIN_DELETED_WINDOW = timedelta(minutes=1)
# deleted children are only visible while in the 15-day Recycle Bin; a child cursor
# older than this reads its parents in full instead
_CHILD_CURSOR_MAX_AGE = timedelta(days=14)
# the client credentials flow needs the org's own My Domain
_GENERIC_LOGIN_HOSTS = {"login.salesforce.com", "test.salesforce.com"}

_COMMENTS = (
    "(SELECT CommentBody, IsPublished, CreatedDate, CreatedBy.Name FROM CaseComments "
    f"ORDER BY CreatedDate DESC, Id DESC LIMIT {_MAX_CHILDREN})"
)
_TASKS = (
    "(SELECT Subject, Description, Status, ActivityDate, CreatedDate FROM Tasks "
    f"ORDER BY CreatedDate DESC, Id DESC LIMIT {_MAX_CHILDREN})"
)
# Only posts people write. Every case comment is mirrored in the feed as a
# CaseCommentPost with an EMPTY body (recorded 2026-10-29), and CreateRecordEvent and
# tracked-change items are system noise; the comment text comes from CaseComments.
# Every subquery breaks CreatedDate ties on Id: two comments on one live case share a
# CreatedDate to the second (recorded 2026-10-30), and an order that can change between
# reads would rewrite an unchanged document.
_FEEDS = (
    "(SELECT Type, Body, IsRichText, CreatedDate, CreatedBy.Name, "
    "(SELECT CommentBody, IsRichText, CreatedDate, CreatedBy.Name FROM FeedComments "
    f"ORDER BY CreatedDate, Id LIMIT {_MAX_REPLIES}) "
    "FROM Feeds WHERE Type IN ('TextPost', 'LinkPost', 'ContentPost') "
    f"ORDER BY CreatedDate DESC, Id DESC LIMIT {_MAX_CHILDREN})"
)
_FIELDS = {
    "Account": [
        "Name",
        "Industry",
        "Type",
        "Description",
        "Website",
        "BillingCity",
        "BillingCountry",
        "Owner.Name",
    ],
    "Contact": ["Name", "Title", "Department", "Description", "Account.Name", "Owner.Name"],
    "Opportunity": [
        "Name",
        "StageName",
        "Amount",
        "CloseDate",
        "Probability",
        "NextStep",
        "Description",
        "IsClosed",
        "IsWon",
        "Account.Name",
        "Owner.Name",
    ],
    "Case": [
        "CaseNumber",
        "Subject",
        "Status",
        "Priority",
        "Origin",
        "Description",
        "IsClosed",
        "Account.Name",
        "Contact.Name",
        "Owner.Name",
    ],
}


class SalesforceSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class SalesforceAPIError(SalesforceSourceError):
    """Salesforce answered with an error or a malformed body."""


class SalesforceUnavailableError(SalesforceAPIError):
    """Salesforce could not be reached, or kept answering 5xx: not about any record."""


class SalesforceAuthError(SalesforceSourceError):
    """Salesforce refused the app's credentials or kept refusing its token.

    No request can succeed, so the run aborts and nothing is deleted.
    """


class SalesforceNotFoundError(SalesforceAPIError):
    """A record that is not there: HTTP 404 with errorCode ``NOT_FOUND``."""


class SalesforceRateLimitedError(SalesforceSourceError):
    """The org's API allowance is used up, or retries ran past this run's wait budget."""


class SalesforceReplicationWindowError(SalesforceAPIError):
    """getDeleted can't cover the window asked for (``INVALID_REPLICATION_DATE``, which
    arrives with several messages); the ids are compared instead."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class SalesforceClient:
    """Salesforce REST client: client-credentials tokens, retries, query paging."""

    def __init__(
        self,
        login_url: str,
        client_id: str,
        client_secret: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        check_active: Callable[[], None] | None = None,
    ):
        if not client_id or not client_secret:
            raise ValueError("Salesforce needs a client_id and a client_secret")
        self.login_url = _login_url(login_url)
        self._client_id = client_id
        self._client_secret = client_secret
        self.api_version = api_version
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        # only a client this object made is this object's to close
        self._owns_http = http is None
        self._sleep = sleep
        self.check_active = check_active
        self._slept = 0.0
        self._token: str | None = None
        self.instance_url: str | None = None
        # the org the token belongs to, read from the token's identity URL
        # (.../id/<org id>/<user id>), so sync state is never reused across orgs
        self.org_id: str | None = None
        # (used, limit) from the last Sforce-Limit-Info header
        self.api_usage: tuple[int, int] | None = None
        # Salesforce's own clock, from the last response's Date header. Every cursor is
        # set from it: records carry Salesforce's stamps, and a cursor taken from a local
        # clock running ahead would skip every record created in the gap
        self._server_time: datetime | None = None

    def reset_wait_budget(self) -> None:
        """Start a run's retry-wait budget afresh (a client can serve several runs)."""
        self._slept = 0.0

    def __repr__(self) -> str:
        return f"SalesforceClient(login_url={self.login_url!r}, api_version={self.api_version!r})"

    def close(self) -> None:
        """Release the connection pool. A caller-supplied client is left alone."""
        if self._owns_http:
            self._http.close()

    @property
    def data_path(self) -> str:
        return f"/services/data/v{self.api_version}"

    def connect(self) -> None:
        """Mint a token now, so the org id is known before any sync state is touched."""
        if self._token is None:
            self._mint()

    def server_now(self) -> datetime:
        """Salesforce's time as of the last response (the local clock only before any)."""
        return self._server_time or datetime.now(timezone.utc).replace(microsecond=0)

    def query_page(
        self, soql: str | None = None, *, next_url: str | None = None, include_deleted: bool = False
    ) -> tuple[list[dict], str | None]:
        """One page of a SOQL query: ``(records, next page URL or None)``.

        Pass ``soql`` for the first page and the returned URL for each one after it.
        ``include_deleted`` uses ``queryAll``, which also returns rows still in the Recycle
        Bin with ``IsDeleted: true`` - how a removed comment or task is noticed.
        """
        if next_url is not None:
            body = self._json("GET", next_url)
        else:
            endpoint = "queryAll" if include_deleted else "query"
            body = self._json("GET", f"{self.data_path}/{endpoint}", params={"q": soql})
        return _query_page(body)

    def get_deleted(self, sobject: str, start: str, end: str) -> dict:
        """``GET /sobjects/<type>/deleted/?start=&end=``, raising on a window it refuses."""
        body = self._json(
            "GET",
            f"{self.data_path}/sobjects/{sobject}/deleted/",
            params={"start": start, "end": end},
        )
        if not isinstance(body, dict) or not isinstance(body.get("deletedRecords"), list):
            raise SalesforceAPIError(
                f"Salesforce sent a malformed deleted-records list for {sobject}"
            )
        return body

    def feed_enabled(self) -> set[str]:
        """The objects with Chatter feed tracking on, from describeGlobal's ``feedEnabled``
        (one request for every object; recorded 2026-10-30)."""
        body = self._json("GET", f"{self.data_path}/sobjects/")
        if not isinstance(body, dict) or not isinstance(body.get("sobjects"), list):
            raise SalesforceAPIError("Salesforce sent a malformed object list")
        return {
            str(item.get("name"))
            for item in body["sobjects"]
            if isinstance(item, dict) and item.get("feedEnabled") is True
        }

    def record_exists(self, sobject: str, record_id: str) -> bool:
        """Whether a single record still answers. Only a 404 ``NOT_FOUND`` says no."""
        try:
            self._json(
                "GET", f"{self.data_path}/sobjects/{sobject}/{record_id}", params={"fields": "Id"}
            )
        except SalesforceNotFoundError:
            return False
        return True

    # -- plumbing -------------------------------------------------------------
    def _mint(self) -> None:
        """A client-credentials token. Salesforce returns no ``expires_in`` and no
        refresh token (recorded 2026-10-29), so a token is re-minted when one is
        refused rather than on a schedule."""
        if self.check_active is not None:
            self.check_active()
        try:
            response = self._http.post(
                self.login_url + "/services/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.TransportError as exc:
            raise SalesforceUnavailableError("Salesforce could not be reached for a token") from exc
        self._note_time(response)
        if response.status_code >= 500:
            raise SalesforceUnavailableError(
                f"Salesforce answered HTTP {response.status_code} for a token"
            )
        body = _json_or_none(response)
        if response.status_code != 200 or not isinstance(body, dict):
            # a wrong secret is 400 {"error": "invalid_client", "error_description":
            # "invalid client credentials"} (recorded); neither field carries a secret
            error = body.get("error") if isinstance(body, dict) else None
            description = body.get("error_description") if isinstance(body, dict) else None
            raise SalesforceAuthError(
                f"Salesforce refused the app's credentials (HTTP {response.status_code}, "
                f"{error or 'no error code'}: {description or 'no description'}); check the "
                "client id and secret, and that the client credentials flow has a Run As user"
            )
        token, instance = body.get("access_token"), body.get("instance_url")
        if not token or not instance:
            raise SalesforceAuthError("Salesforce sent a token response without a token")
        org_id = _org_id(body.get("id"))
        if org_id is None:
            # sync state is keyed on the org; without it two orgs could share one state
            raise SalesforceAuthError("Salesforce sent a token response that names no org")
        self._token = token
        self.instance_url = str(instance).rstrip("/")
        self.org_id = org_id

    def _json(self, method: str, path: str, *, params: dict | None = None) -> Any:
        response = self._send(method, path, params=params)
        return self._parse(response, path)

    def _send(self, method: str, path: str, *, params: dict | None = None) -> httpx.Response:
        if self._token is None:
            self._mint()
        failures = reminted = 0
        while True:
            if self.check_active is not None:
                self.check_active()
            headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
            try:
                response = self._http.request(
                    method, f"{self.instance_url}{path}", params=params, headers=headers
                )
            except httpx.TransportError as exc:
                failures += 1
                if failures >= _RETRIES:
                    raise SalesforceUnavailableError(
                        f"Salesforce could not be reached for {_endpoint(path)}"
                    ) from exc
                self._pause(2 ** (failures - 1), path)
                continue
            self._note_usage(response)
            self._note_time(response)
            if response.status_code == 401 and _error_code(response) == "INVALID_SESSION_ID":
                # tokens carry no lifetime, so an expired one shows up here; one fresh
                # token per request, and a second refusal is a real auth failure
                if reminted:
                    raise SalesforceAuthError(
                        f"Salesforce refused a fresh token for {_endpoint(path)} (HTTP 401)"
                    )
                reminted += 1
                self._mint()
                continue
            if response.status_code >= 500:
                failures += 1
                if failures < _RETRIES:
                    self._pause(2 ** (failures - 1), path)
                    continue
            return response

    def _pause(self, delay: float, path: str) -> None:
        if self._slept + delay > _MAX_TOTAL_WAIT:
            raise SalesforceUnavailableError(
                f"Salesforce kept failing for {_endpoint(path)}; retries ran past this run's "
                f"{_MAX_TOTAL_WAIT:.0f}s budget"
            )
        if self.check_active is not None:
            self.check_active()
        self._slept += delay
        self._sleep(delay)

    def _note_time(self, response: httpx.Response) -> None:
        try:
            moment = parsedate_to_datetime(response.headers.get("date", ""))
        except (TypeError, ValueError):
            return
        if moment is not None and moment.tzinfo is not None:
            self._server_time = moment.astimezone(timezone.utc).replace(microsecond=0)

    def _note_usage(self, response: httpx.Response) -> None:
        """``Sforce-Limit-Info: api-usage=<used>/<limit>``, the org's rolling 24h count."""
        match = re.search(r"api-usage=(\d+)/(\d+)", response.headers.get("sforce-limit-info", ""))
        if match:
            self.api_usage = (int(match.group(1)), int(match.group(2)))

    @staticmethod
    def _parse(response: httpx.Response, path: str) -> Any:
        status = response.status_code
        endpoint = _endpoint(path)
        code = _error_code(response)
        if status == 401:
            raise SalesforceAuthError(
                f"Salesforce refused {endpoint} (HTTP 401, {code or 'no code'})"
            )
        if status == 403 and code == "REQUEST_LIMIT_EXCEEDED":
            # the same code covers the daily allowance and the concurrent-request limit
            raise SalesforceRateLimitedError(
                f"Salesforce refused {endpoint} with REQUEST_LIMIT_EXCEEDED (the org's daily "
                "API allowance or its concurrent-request limit); the run stops and resumes "
                "next time"
            )
        if status == 404 and code == "NOT_FOUND":
            # recorded 2026-10-29: the same body for a deleted record and for an id that
            # never existed - a NOT_FOUND is only ever read as "not there"
            raise SalesforceNotFoundError(f"Salesforce has no record at {endpoint}")
        if status == 400 and code == "INVALID_REPLICATION_DATE":
            raise SalesforceReplicationWindowError(
                f"Salesforce can't list deletes for that window ({_error_message(response)})"
            )
        if (
            status == 400
            and code == "INVALID_TYPE"
            and re.search(r"'(Task|Event)'", _error_message(response))
        ):
            raise SalesforceAPIError(
                "this Salesforce user can't read Tasks; grant it the 'Access Activities' "
                "permission, or pass include_tasks=False"
            )
        if status >= 500:
            raise SalesforceUnavailableError(f"Salesforce answered HTTP {status} for {endpoint}")
        if status >= 400:
            raise SalesforceAPIError(
                f"Salesforce answered HTTP {status} for {endpoint}: "
                f"{code or 'no code'} {_error_message(response)!r}"
            )
        body = _json_or_none(response)
        if body is None:
            raise SalesforceAPIError(f"Salesforce sent a non-JSON body for {endpoint}")
        return body


def _query_page(body: Any) -> tuple[list[dict], str | None]:
    """``(records, next page URL)`` of one query response, rejecting malformed ones.

    A malformed page must never read as "no records": that would look like every stored
    record had been deleted.
    """
    if not isinstance(body, dict) or not isinstance(body.get("records"), list):
        raise SalesforceAPIError("Salesforce sent a query page without a records list")
    if not isinstance(body.get("done"), bool):
        raise SalesforceAPIError("Salesforce sent a query page without a done flag")
    records = body["records"]
    if any(not isinstance(record, dict) for record in records):
        raise SalesforceAPIError("Salesforce sent a query page with non-object records")
    if body["done"]:
        return records, None
    next_url = body.get("nextRecordsUrl")
    if not isinstance(next_url, str) or not next_url.startswith("/services/data/"):
        # an unfinished listing with no way on would otherwise end as if complete
        raise SalesforceAPIError("Salesforce sent an unfinished query page without a next page")
    return records, next_url


def _login_url(value: str) -> str:
    url = (value or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(
            "login_url must be the org's https My Domain URL, e.g. https://acme.my.salesforce.com"
        )
    if parts.hostname in _GENERIC_LOGIN_HOSTS:
        raise ValueError(
            "login_url must be the org's own My Domain URL (Setup > My Domain), not "
            f"{parts.hostname}: the client credentials flow only works against My Domain"
        )
    return f"https://{parts.hostname}"


def _org_id(identity_url: Any) -> str | None:
    """The org id in a token's identity URL: ``https://login.salesforce.com/id/<org>/<user>``."""
    parts = str(identity_url or "").rstrip("/").split("/")
    return parts[-2] if len(parts) >= 3 and parts[-3] == "id" else None


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _first_error(response: httpx.Response) -> dict:
    """Salesforce errors are a JSON *list* of ``{message, errorCode, fields}``."""
    body = _json_or_none(response)
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body[0]
    return body if isinstance(body, dict) else {}


def _error_code(response: httpx.Response) -> str:
    return str(_first_error(response).get("errorCode") or "")


def _error_message(response: httpx.Response) -> str:
    return str(_first_error(response).get("message") or "")[:300]


def _endpoint(path: str) -> str:
    """``path`` with record ids and query locators replaced, for messages and logs."""
    path = path.split("?", 1)[0]
    path = re.sub(r"/query(All)?/[^/]+", r"/query\1/{locator}", path)
    return re.sub(r"/(sobjects/\w+)/[A-Za-z0-9]{15,18}", r"/\1/{id}", path)


# ---------------------------------------------------------------------------
# SOQL
# ---------------------------------------------------------------------------
def parent_soql(
    sobject: str,
    *,
    since: str | None = None,
    ids: list[str] | None = None,
    id_field: str = "Id",
    where: str | None = None,
    comments: bool = True,
    tasks: bool = True,
    chatter: bool = True,
) -> str:
    """The query for one object's documents: its fields plus its child text.

    ``ids`` narrows it to records whose ``id_field`` (``Id``, or a lookup such as
    ``AccountId``) is one of them.
    """
    columns = ["Id", "SystemModstamp", *_FIELDS[sobject]]
    if comments and sobject == "Case":
        columns.append(_COMMENTS)
    if tasks:
        columns.append(_TASKS)
    if chatter:
        columns.append(_FEEDS)
    conditions = []
    if since:
        conditions.append(f"SystemModstamp >= {since}")
    if ids is not None:
        quoted = ", ".join(f"'{_record_id(i)}'" for i in ids)
        conditions.append(f"{id_field} IN ({quoted})")
    if where:
        conditions.append(f"({where})")
    soql = f"SELECT {', '.join(columns)} FROM {sobject}"
    if conditions:
        soql += " WHERE " + " AND ".join(conditions)
    return soql + " ORDER BY SystemModstamp, Id"


def ids_soql(sobject: str, *, where: str | None = None) -> str:
    """Every selected id of one object, to compare with the stored rows."""
    soql = f"SELECT Id FROM {sobject}"
    return soql + f" WHERE ({where})" if where else soql


def child_soql(stream: str, since: str) -> str:
    """Changed child rows of one stream, to find the parents whose documents changed."""
    # a task points at its record through WhatId, or at a contact through WhoId; comments
    # and feed items through ParentId
    columns = "WhatId, WhoId" if stream == "Task" else "ParentId"
    return (
        f"SELECT {columns}, SystemModstamp FROM {stream} "
        f"WHERE SystemModstamp >= {since} ORDER BY SystemModstamp, Id"
    )


def _record_id(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9]{15,18}", text):
        raise SalesforceAPIError(f"Salesforce sent a malformed record id {text[:20]!r}")
    return text


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_record(sobject: str, record: dict) -> dict:
    """Turn one record (with its child subqueries) into a document row."""
    record_id = _record_id(record.get("Id"))
    title = _clipped(_title(sobject, record), _MAX_SHORT)
    lines = [title]
    if facts := _facts(sobject, record):
        lines.append(" · ".join(facts))
    if people := _people(sobject, record):
        lines.append(" · ".join(people))
    for label, field in (("Next step", "NextStep"), ("Description", "Description")):
        if value := _text(record.get(field)):
            lines.append(f"{label}: {_clipped(value, _MAX_TEXT)}")
    lines += _comment_lines(record)
    lines += _task_lines(record)
    lines += _feed_lines(record)
    return {
        "id": _row_id(sobject, record_id),
        "title": title,
        "content": "\n".join(lines),
        # one node set per object type, so their number is bounded by the four object
        # types, never by the number of records
        NODE_SET_COLUMN: [f"salesforce:{sobject.lower()}"],
        "_deleted": False,
    }


def _title(sobject: str, record: dict) -> str:
    if sobject == "Case":
        number = _text(record.get("CaseNumber"))
        subject = _text(record.get("Subject")) or "(no subject)"
        return f"Case {number}: {subject}" if number else f"Case: {subject}"
    name = _text(record.get("Name")) or f"(unnamed {sobject.lower()})"
    return f"{sobject}: {name}"


def _facts(sobject: str, record: dict) -> list[str]:
    facts = []
    if sobject == "Account":
        for label, field in (("Industry", "Industry"), ("Type", "Type"), ("Website", "Website")):
            if value := _text(record.get(field)):
                facts.append(f"{label}: {value}")
        place = ", ".join(
            v for v in (_text(record.get("BillingCity")), _text(record.get("BillingCountry"))) if v
        )
        if place:
            facts.append(f"Location: {place}")
    elif sobject == "Contact":
        for label, field in (("Title", "Title"), ("Department", "Department")):
            if value := _text(record.get(field)):
                facts.append(f"{label}: {value}")
    elif sobject == "Opportunity":
        if stage := _text(record.get("StageName")):
            facts.append(f"Stage: {stage}")
        if record.get("Amount") is not None:
            facts.append(f"Amount: {record['Amount']}")
        if close := _text(record.get("CloseDate")):
            facts.append(f"Close date: {close}")
        if record.get("Probability") is not None:
            facts.append(f"Probability: {record['Probability']}%")
        if record.get("IsClosed"):
            facts.append("Closed won" if record.get("IsWon") else "Closed lost")
    elif sobject == "Case":
        for label, field in (("Status", "Status"), ("Priority", "Priority"), ("Origin", "Origin")):
            if value := _text(record.get(field)):
                facts.append(f"{label}: {value}")
        facts.append("Closed" if record.get("IsClosed") else "Open")
    return facts


def _people(sobject: str, record: dict) -> list[str]:
    people = []
    for label, relation in (("Account", "Account"), ("Contact", "Contact"), ("Owner", "Owner")):
        if relation == sobject:
            continue
        if name := _related_name(record, relation):
            people.append(f"{label}: {name}")
    return people


def _related_name(record: dict, relation: str) -> str:
    related = record.get(relation)
    return _text(related.get("Name")) if isinstance(related, dict) else ""


def _child_rows(record: dict, relation: str) -> list[dict]:
    """A child subquery's rows. Salesforce sends ``null``, not an empty result, for a
    record with no children (recorded 2026-10-29)."""
    sub = record.get(relation)
    if sub is None:
        return []
    if not isinstance(sub, dict) or not isinstance(sub.get("records"), list):
        raise SalesforceAPIError(f"Salesforce sent a malformed {relation} subquery")
    return [row for row in sub["records"] if isinstance(row, dict)]


def _comment_lines(record: dict) -> list[str]:
    rows = _child_rows(record, "CaseComments")
    if not rows:
        return []
    lines = [f"Case comments ({len(rows)}, newest first):"]
    for row in rows:
        visibility = "public" if row.get("IsPublished") else "internal"
        author = _related_name(row, "CreatedBy")
        head = f"{_date(row.get('CreatedDate'))} ({visibility}{', ' + author if author else ''})"
        lines.append(f"  {head}: {_clipped(_text(row.get('CommentBody')), _MAX_TEXT)}")
    return lines


def _task_lines(record: dict) -> list[str]:
    rows = _child_rows(record, "Tasks")
    if not rows:
        return []
    lines = [f"Tasks ({len(rows)}, newest first):"]
    for row in rows:
        when = _text(row.get("ActivityDate")) or _date(row.get("CreatedDate"))
        status = _text(row.get("Status"))
        subject = _clipped(_text(row.get("Subject")), _MAX_SHORT)
        line = f"  {when}{' ' + status if status else ''}: {subject}"
        if description := _text(row.get("Description")):
            line += f" - {_clipped(description, _MAX_TEXT)}"
        lines.append(line)
    return lines


def _feed_lines(record: dict) -> list[str]:
    rows = [row for row in _child_rows(record, "Feeds") if _text(row.get("Body"))]
    if not rows:
        return []
    lines = [f"Chatter ({len(rows)} post{'' if len(rows) == 1 else 's'}, newest first):"]
    for row in rows:
        author = _related_name(row, "CreatedBy") or "someone"
        body = _clipped(_plain(row.get("Body"), row.get("IsRichText")), _MAX_TEXT)
        lines.append(f"  {_date(row.get('CreatedDate'))} {author}: {body}")
        for reply in _child_rows(row, "FeedComments"):
            who = _related_name(reply, "CreatedBy") or "someone"
            text = _plain(reply.get("CommentBody"), reply.get("IsRichText"))
            lines.append(f"    reply {who}: {_clipped(text, _MAX_TEXT)}")
    return lines


def _plain(value: Any, rich: Any) -> str:
    """A Chatter body's words. Posts written in the UI are rich text (``IsRichText``),
    whose tags go and whose entities (``&amp;``) become characters; a plain-text body,
    such as one posted through the API (recorded), is kept as written."""
    text = _text(value)
    if rich is True:
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def _clipped(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, the marker included, marked when cut."""
    if len(text) <= limit:
        return text
    marker = f"… [{len(text) - limit} more characters]"
    return text[: max(0, limit - len(marker))].rstrip() + marker


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _row_id(sobject: str, record_id: str) -> str:
    return f"{sobject.lower()}:{record_id}"


def _split_row_id(row_id: str) -> tuple[str, str]:
    prefix, _, record_id = row_id.partition(":")
    for sobject in SUPPORTED_OBJECTS:
        if sobject.lower() == prefix:
            return sobject, record_id
    raise SalesforceSourceError(f"unexpected stored row id {row_id!r}")


def _hash(value: Any) -> str:
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:16]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    text = _OFFSET.sub(r"\1:\2", text.replace("Z", "+00:00"))
    # Python 3.10's fromisoformat only takes 3 or 6 fractional digits
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _stamp(value: Any) -> str | None:
    moment = _parse_time(value)
    return _iso(moment) if moment else None


def _date(value: Any) -> str:
    stamp = _stamp(value)
    return stamp[:10] if stamp else ""


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
# Records whose name appears in other records' documents (Account.Name on contacts,
# opportunities and cases; Contact.Name on cases), with the field that points at them.
# A rename moves only the renamed record's SystemModstamp, not its dependents'.
_DEPENDENTS = {
    "Account": (("Contact", "AccountId"), ("Opportunity", "AccountId"), ("Case", "AccountId")),
    "Contact": (("Case", "ContactId"),),
}


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the state reached so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _fingerprint(row: dict) -> str:
    """The title's hash, then the whole row's: a changed title (a rename) is told apart."""
    return _hash(row["title"])[:8] + _hash([row["title"], row["content"], row[NODE_SET_COLUMN]])


class _Walker:
    """One run, writing its progress into ``state`` (dlt resource state, or a dict)."""

    def __init__(
        self,
        client: Any,
        state: dict,
        stats: dict,
        *,
        objects: tuple[str, ...] = SUPPORTED_OBJECTS,
        where: dict[str, str] | None = None,
        comments: bool = True,
        tasks: bool = True,
        chatter: bool = True,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)
        stats.setdefault("completed", False)
        self.objects = tuple(objects)
        self.where = dict(where or {})
        self.comments, self.tasks, self.chatter = comments, tasks, chatter
        self.max_requests = max_requests
        self.requests = 0
        self.scope = _hash(
            [sorted(self.objects), sorted(self.where.items()), comments, tasks, chatter]
        )
        # set by _prepare_state: Salesforce's time as the run began, whether anything was
        # stored before it, the objects with feed tracking on, the objects read in full,
        # and the objects whose delete cursor starts this run
        self.run_start = datetime.min.replace(tzinfo=timezone.utc)
        self.first_sync = True
        self.feeds: tuple[str, ...] = ()
        self.read_in_full: set[str] = set()
        self.new_delete_cursor: set[str] = set()
        # {row id: fingerprint of the rendered row}
        self.rows: dict[str, str] = state.setdefault("rows", {})
        # {object: stamp}: parent records read up to here
        self.cursors: dict[str, str] = state.setdefault("cursors", {})
        # {child stream: stamp}: CaseComment, Task, <Object>Feed
        self.child_cursors: dict[str, str] = state.setdefault("child_cursors", {})
        # {object: start of the next getDeleted window}
        self.deleted_cursors: dict[str, str] = state.setdefault("deleted_cursors", {})
        # The next two are kept in state so that a run cut short still finishes them:
        # objects whose ids must be compared in full (a refused deleted-records window, or
        # a changed selection) ...
        self.to_reconcile: list[str] = state.setdefault("reconcile", [])
        # ... and {Account or Contact: ids renamed, deleted or restored}, whose dependents
        # must be rendered again
        self.renamed: dict[str, list[str]] = state.setdefault("renamed", {})

    # -- requests -------------------------------------------------------------
    def _spend_request(self) -> None:
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        usage = self.client.api_usage
        if usage and usage[0] >= usage[1] * (1 - _ALLOWANCE_RESERVE):
            raise _CutShortError("api_limit")
        self.requests += 1

    def _pages(self, soql: str, *, include_deleted: bool = False) -> Iterator[list[dict]]:
        """Each page of a query, one budgeted request per page actually fetched."""
        next_url: str | None = None
        seen: set[str] = set()
        while True:
            self._spend_request()
            if next_url is None:
                records, next_url = self.client.query_page(soql, include_deleted=include_deleted)
            else:
                records, next_url = self.client.query_page(next_url=next_url)
            yield records
            if next_url is None:
                return
            if next_url in seen:
                # the same locator again would re-read this page until the budget is gone
                raise SalesforceAPIError("Salesforce returned the same query page twice")
            seen.add(next_url)

    # -- the run --------------------------------------------------------------
    def run(self) -> Iterator[dict]:
        for row_id in self._prepare_state():
            yield self._tombstone(row_id, dropped=True)
        for sobject in self.objects:
            yield from self._read_changed(sobject)
        for stream in self._child_streams():
            if set(self._parents_fed_by(stream)) <= self.read_in_full:
                # every parent was just read in full, children included
                self._advance_child_cursor(stream)
            else:
                yield from self._read_changed_children(stream)
        for sobject in self.objects:
            if sobject in self.to_reconcile:
                yield from self._reconcile_ids(sobject)
            elif sobject in self.new_delete_cursor:
                continue  # read in full just now; deletes are tracked from here on
            elif sobject in self.where:
                # a filtered object is listed every run: a record can leave the filter
                # without its SystemModstamp moving (a formula field, a relative date)
                yield from self._reconcile_ids(sobject)
            else:
                yield from self._read_deletes(sobject)
        yield from self._rerender_dependents()

    def _prepare_state(self) -> list[str]:
        """Check the state belongs to this org and selection, and set up the run.

        Returns the stored rows of objects no longer selected, to be forgotten.
        """
        if self.client.org_id is None:
            self._spend_request()
            self.client.connect()
        self.client.reset_wait_budget()
        self.run_start = self.client.server_now()
        known = self.state.get("org")
        if known and known != self.client.org_id:
            raise SalesforceSourceError(
                "this resource_name already holds another Salesforce org's sync state; give "
                "each org its own resource_name"
            )
        self.state["org"] = self.client.org_id
        self.first_sync = not self.rows
        if self.chatter:
            # feed tracking is per object and can be off, and an untracked object has no
            # feed to query; describeGlobal reports feedEnabled for all of them at once
            self._spend_request()
            enabled = self.client.feed_enabled()
            self.feeds = tuple(o for o in self.objects if o in enabled)

        dropped: list[str] = []
        previous = self.state.get("scope")
        if previous is not None and previous != self.scope:
            # objects no longer selected are forgotten; the rest are read again in full and
            # their ids compared, which catches records that left a narrowed filter
            dropped = [r for r in self.rows if _split_row_id(r)[0] not in self.objects]
            self.cursors.clear()
            self.child_cursors.clear()
            self.deleted_cursors.clear()
            self.renamed.clear()
            self.to_reconcile[:] = list(self.objects)
        self.state["scope"] = self.scope
        for sobject in set(self.state.get("feeds", self.feeds)) ^ set(self.feeds):
            # feed tracking turned on or off: the object's documents gain or lose Chatter
            self.cursors.pop(sobject, None)
        self.state["feeds"] = list(self.feeds)

        start = _iso(self.run_start - _CURSOR_OVERLAP)
        for stream in self._child_streams():
            cursor = self.child_cursors.get(stream)
            if cursor and _parse_time(cursor) < self.run_start - _CHILD_CURSOR_MAX_AGE:
                # a deleted child is only seen while it sits in the Recycle Bin; after a gap
                # that long, read its parents in full instead
                for sobject in self._parents_fed_by(stream):
                    self.cursors.pop(sobject, None)
                cursor = None
            # started before any parent is read: a run cut short during a full read must
            # still catch the children that change from here on
            self.child_cursors[stream] = cursor or start
        self.read_in_full = {o for o in self.objects if o not in self.cursors}
        for sobject in self.objects:
            if sobject not in self.deleted_cursors:
                self.deleted_cursors[sobject] = _iso(self.run_start)
                self.new_delete_cursor.add(sobject)
        return dropped

    def _child_streams(self) -> list[str]:
        streams = []
        if self.comments and "Case" in self.objects:
            streams.append("CaseComment")
        if self.tasks:
            streams.append("Task")
        return streams + [f"{sobject}Feed" for sobject in self.feeds]

    def _parents_fed_by(self, stream: str) -> tuple[str, ...]:
        if stream == "Task":
            return self.objects
        if stream == "CaseComment":
            return ("Case",)
        return (stream.removesuffix("Feed"),)

    def _parent_soql(
        self,
        sobject: str,
        *,
        since: str | None = None,
        ids: list[str] | None = None,
        id_field: str = "Id",
    ) -> str:
        return parent_soql(
            sobject,
            since=since,
            ids=ids,
            id_field=id_field,
            where=self.where.get(sobject),
            comments=self.comments,
            tasks=self.tasks,
            chatter=sobject in self.feeds,
        )

    def _row_if_changed(self, sobject: str, record: dict) -> dict | None:
        """The row to emit for one record, or None when its document did not change."""
        self.stats["scanned"] += 1
        try:
            row = render_record(sobject, record)
        except SalesforceAPIError as exc:
            self.stats["failed"] += 1
            logger.warning("Salesforce sent a record that can't be rendered: %s", exc)
            return None
        fingerprint = _fingerprint(row)
        stored = self.rows.get(row["id"])
        if stored == fingerprint:
            self.stats["skipped"] += 1
            return None
        if stored is None or stored[:8] != fingerprint[:8]:
            # a new title (a rename) or a record that is back (an undelete)
            self._queue_dependents(sobject, row["id"])
        self.rows[row["id"]] = fingerprint
        return row

    def _queue_dependents(self, sobject: str, row_id: str) -> None:
        # nothing shows a name yet on a first sync, which reads every record anyway
        if sobject in _DEPENDENTS and not self.first_sync:
            ids = self.renamed.setdefault(sobject, [])
            record_id = _split_row_id(row_id)[1]
            if record_id not in ids:
                ids.append(record_id)

    def _read_changed(self, sobject: str) -> Iterator[dict]:
        """Records of one object changed since its cursor (all of them without one)."""
        cursor = self.cursors.get(sobject)
        since = _iso(_parse_time(cursor) - _CURSOR_OVERLAP) if cursor else None
        for page in self._pages(self._parent_soql(sobject, since=since)):
            newest = cursor
            for record in page:
                if row := self._row_if_changed(sobject, record):
                    yield row
                stamp = _stamp(record.get("SystemModstamp"))
                if stamp and (newest is None or stamp > newest):
                    newest = stamp
            # a page is fully yielded before the cursor moves past it
            if newest:
                self.cursors[sobject] = newest
        if not cursor:
            # an empty object still starts its cursor, so the next run is incremental
            self.cursors.setdefault(sobject, _iso(self.run_start - _CURSOR_OVERLAP))

    def _read_changed_children(self, stream: str) -> Iterator[dict]:
        """Child rows (comments, tasks, posts) changed since their cursor, re-rendering
        each parent: a new comment does NOT move its Case's SystemModstamp (recorded
        2026-10-29), so the parent's own cursor never sees it."""
        cursor = self.child_cursors[stream]
        since = _iso(_parse_time(cursor) - _CURSOR_OVERLAP)
        # queryAll: a deleted comment or task must also change its parent's document
        for page in self._pages(child_soql(stream, since), include_deleted=True):
            parents: dict[str, set[str]] = defaultdict(set)
            newest = cursor
            for row in page:
                for sobject, record_id in self._parent_ids(stream, row):
                    parents[sobject].add(record_id)
                stamp = _stamp(row.get("SystemModstamp"))
                if stamp and stamp > newest:
                    newest = stamp
            for sobject, ids in parents.items():
                yield from self._rerender(sobject, sorted(ids))
            self.child_cursors[stream] = newest
        self._advance_child_cursor(stream)

    def _advance_child_cursor(self, stream: str) -> None:
        """After a full pass, a quiet stream's cursor moves up to this run too; one that
        only moved with its rows would look stale after two quiet weeks and force a full
        read of its parents."""
        start = _iso(self.run_start - _CURSOR_OVERLAP)
        self.child_cursors[stream] = max(self.child_cursors[stream], start)

    def _parent_ids(self, stream: str, row: dict) -> list[tuple[str, str]]:
        if stream == "Task":
            candidates = [row.get("WhatId"), row.get("WhoId")]
        else:
            candidates = [row.get("ParentId")]
        found = []
        for value in candidates:
            record_id = _text(value)
            sobject = _KEY_PREFIX.get(record_id[:3])
            if sobject in self.objects:
                found.append((sobject, record_id))
        return found

    def _rerender_dependents(self) -> Iterator[dict]:
        """Documents that show the name of an Account or Contact that changed, 200 names
        at a time; each finished batch leaves the list, so a run cut short resumes."""
        while self.renamed:
            parent = next(iter(self.renamed))
            ids = self.renamed[parent]
            chunk = sorted(ids)[:_ID_CHUNK]
            for sobject, field in _DEPENDENTS[parent]:
                if sobject in self.objects:
                    yield from self._rerender(sobject, chunk, id_field=field)
            ids[:] = [i for i in ids if i not in chunk]
            if not ids:
                del self.renamed[parent]

    def _rerender(self, sobject: str, ids: list[str], *, id_field: str = "Id") -> Iterator[dict]:
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start : start + _ID_CHUNK]
            for page in self._pages(self._parent_soql(sobject, ids=chunk, id_field=id_field)):
                for record in page:
                    if row := self._row_if_changed(sobject, record):
                        yield row

    def _read_deletes(self, sobject: str) -> Iterator[dict]:
        """Records Salesforce reports deleted since this object's delete cursor."""
        start = self.deleted_cursors[sobject]
        started = _parse_time(start)
        end_time = self.client.server_now()
        end = _iso(end_time)
        if started < self.run_start - _DELETED_HORIZON:
            # too old for getDeleted (30 days) and maybe for the Recycle Bin (15)
            yield from self._reconcile_ids(sobject)
            return
        if started > end_time - _MIN_DELETED_WINDOW:
            return  # too short to ask for; the next run covers it
        self._spend_request()
        try:
            body = self.client.get_deleted(sobject, start, end)
        except SalesforceReplicationWindowError as exc:
            logger.info("Salesforce can't list deletes for %s (%s); comparing ids", sobject, exc)
            yield from self._reconcile_ids(sobject)
            return
        earliest = _stamp(body.get("earliestDateAvailable"))
        if earliest and earliest > start:
            # deletes before `earliest` are no longer listed (Recycle Bin purged)
            yield from self._reconcile_ids(sobject)
            return
        for entry in body["deletedRecords"]:
            if not isinstance(entry, dict):
                raise SalesforceAPIError(
                    f"Salesforce sent a malformed deleted record for {sobject}"
                )
            row_id = _row_id(sobject, _text(entry.get("id")))
            if row_id in self.rows:
                yield self._tombstone(row_id)
        # latestDateCovered is "guaranteed complete up to": on a live org it lagged hours
        # behind (17:52 at 20:34) while newer deletes were already listed. The next window
        # starts there - re-reported deletes are harmless - but never before the earliest
        # date Salesforce still has, which on a new org is LATER than latestDateCovered.
        covered = _stamp(body.get("latestDateCovered")) or start
        floor = earliest or covered
        self.deleted_cursors[sobject] = min(max(covered, floor), end)

    def _reconcile_ids(self, sobject: str) -> Iterator[dict]:
        """List every selected id of an object and forget the stored records it misses.

        Without a filter, a missing record is fetched by id first and only a 404 forgets
        it. With one, the listing *is* the selection: a record it misses was deleted or no
        longer matches, and either way is not selected.
        """
        where = self.where.get(sobject)
        listed: set[str] = set()
        for page in self._pages(ids_soql(sobject, where=where)):
            listed.update(_text(record.get("Id")) for record in page)
        prefix = f"{sobject.lower()}:"
        stored = {row_id[len(prefix) :] for row_id in self.rows if row_id.startswith(prefix)}
        for record_id in sorted(stored - listed):
            if where or not self._still_served(sobject, record_id):
                yield self._tombstone(_row_id(sobject, record_id))
        if where:
            # listed but never read: it entered the filter without its stamp moving
            yield from self._rerender(sobject, sorted(listed - stored))
        if sobject in self.to_reconcile:
            self.to_reconcile.remove(sobject)
        self.deleted_cursors[sobject] = _iso(self.run_start)

    def _still_served(self, sobject: str, record_id: str) -> bool:
        self._spend_request()
        try:
            exists = self.client.record_exists(sobject, record_id)
        except SalesforceAPIError as exc:
            # anything but a 404 - a 5xx included - says nothing about the record, so it
            # stays; raising instead would let one record that keeps failing stop every run
            self.stats["failed"] += 1
            logger.warning(
                "Can't tell whether %s %s still exists (%s); keeping it", sobject, record_id, exc
            )
            return True
        if exists:
            logger.warning(
                "Salesforce still serves %s %s but did not list it; keeping it", sobject, record_id
            )
        return exists

    def _tombstone(self, row_id: str, *, dropped: bool = False) -> dict:
        self.rows.pop(row_id, None)
        self.stats["deleted"] += 1
        if not dropped:
            # a deleted account's cases may still point at it, showing a name now gone
            self._queue_dependents(_split_row_id(row_id)[0], row_id)
        return {"id": row_id, "_deleted": True}


def _iter_rows(client: Any, state: dict, stats: dict, **kwargs: Any) -> Iterator[dict]:
    """Yield the changed rows of a Salesforce org. Independent of dlt, so tests can
    drive it with a plain dict."""
    walker = _Walker(client, state, stats, **kwargs)
    try:
        try:
            yield from walker.run()
        except SalesforceRateLimitedError:
            # Salesforce refused a request for its limits, wherever in the run
            raise _CutShortError("rate_limit") from None
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        stats["completed"] = False
        logger.warning(
            "Salesforce sync stopped early (%s) and did not finish; it resumes next run.",
            cut.reason,
        )
    except SalesforceAuthError:
        # recorded as well as raised: the raise keeps dlt from committing anything
        stats["failed"] = max(1, stats.get("failed", 0))
        stats["failed_auth"] = 1
        raise
    else:
        stats["completed"] = True


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def salesforce_source(
    *,
    login_url: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    resource_name: str | None = None,
    objects: list[str] | None = None,
    where: dict[str, str] | None = None,
    include_comments: bool = True,
    include_tasks: bool = True,
    include_chatter: bool = True,
    api_version: str = DEFAULT_API_VERSION,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource that yields a Salesforce org's records for ``remember``.

    Args:
        login_url: The org's My Domain URL (Setup > My Domain), e.g.
            ``https://acme.my.salesforce.com``. The client credentials flow does not work
            against login.salesforce.com.
        client_id: Consumer key of an External Client App with the client credentials
            flow enabled and a Run As user.
        client_secret: That app's consumer secret.
        resource_name: Stable name of this connection. It scopes the sync state and the
            cleanup of forgotten records, so keep it fixed. Defaults to
            ``salesforce_<my domain>``. Two sources with different selections of one org
            in one dataset each need their own.
        objects: Which of Account, Contact, Opportunity and Case to sync (default: all).
        where: An optional SOQL condition per object, e.g. ``{"Case": "IsClosed = false"}``;
            records that stop matching are forgotten.
        include_comments: Case comments in each case's document.
        include_tasks: Tasks in their account's, contact's, opportunity's or case's
            document. Needs the "Access Activities" permission on the Run As user.
        include_chatter: Chatter posts and their replies in their record's document.
        api_version: Salesforce REST API version, e.g. ``"66.0"``.
        max_requests: Request budget of one run. A big backfill finishes over several runs.
            It must fit one pass of the selection: a token, the object list, a page per
            object and per child stream, and a deleted-records call per object.
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each request.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.

    A run stopped early by ``max_requests`` or by the org's API allowance **returns
    normally**: the rows it read are kept and the next run resumes. That is only visible in
    ``cognee_sync_stats``, which then carries ``completed: False`` and ``failed_budget``,
    ``failed_api_limit`` or ``failed_rate_limit``. A refused credential, a malformed answer
    or an outage raises, and dlt then commits neither rows nor state.
    """
    import dlt

    found = getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    if found < 2:
        raise RuntimeError(
            "Salesforce sync needs cognee's document sync version 2 or later (cognee 1.6.4+), "
            f"for per-row node sets; this cognee reports {found}."
        )
    for flag, value in (
        ("include_comments", include_comments),
        ("include_tasks", include_tasks),
        ("include_chatter", include_chatter),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{flag} must be True or False")
    if not isinstance(max_requests, int) or isinstance(max_requests, bool):
        raise ValueError("max_requests must be a whole number")
    if login_url is not None and not isinstance(login_url, str):
        raise ValueError("login_url must be a string, e.g. https://acme.my.salesforce.com")
    selected = _selected_objects(objects)
    filters = _filters(where, selected)
    if not isinstance(api_version, str) or not _API_VERSION.match(api_version):
        raise ValueError('api_version must look like "66.0"')
    floor = _request_floor(selected, include_comments, include_tasks, include_chatter)
    if max_requests < floor:
        # a budget that can't fit one pass would stop at the same place on every run
        raise ValueError(f"max_requests must be at least {floor} for this selection")
    if service is None:
        if not client_id or not client_secret:
            raise ValueError(
                "salesforce_source needs client_id and client_secret, or a prepared client in "
                "service="
            )
        login = _login_url(login_url or "")
    else:
        login = getattr(service, "login_url", None) or _login_url(login_url or "")
    name = resource_name if resource_name is not None else _default_resource_name(login)
    _check_resource_name(name)

    def _client() -> Any:
        """A client per run, so its connection pool does not outlive the run."""
        return SalesforceClient(
            login,
            client_id or "",
            client_secret or "",
            api_version=api_version,
            check_active=check_active,
        )

    stats: dict = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def salesforce_records():
        stats.clear()
        client = service if service is not None else _client()
        try:
            rows = _iter_rows(
                client,
                dlt.current.resource_state(),
                stats,
                objects=selected,
                where=filters,
                comments=include_comments,
                tasks=include_tasks,
                chatter=include_chatter,
                max_requests=max_requests,
            )
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            # a run that raised still gives its sockets back; an injected client belongs
            # to the caller (the tests), so it is left open
            if service is None:
                client.close()

    resource = salesforce_records()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "salesforce")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource


def _selected_objects(objects: Any) -> tuple[str, ...]:
    if objects is None:
        return SUPPORTED_OBJECTS
    if isinstance(objects, str) or not isinstance(objects, (list, tuple)):
        # tuple("Case") is ("C", "a", "s", "e"): refuse a bare string outright
        raise ValueError('objects must be a list, e.g. ["Case"], not a string')
    unknown = [o for o in objects if o not in SUPPORTED_OBJECTS]
    if unknown or not objects:
        raise ValueError(
            f"objects must be a non-empty list drawn from {list(SUPPORTED_OBJECTS)}; "
            f"got {list(objects)!r}"
        )
    return tuple(o for o in SUPPORTED_OBJECTS if o in objects)


def _filters(where: Any, selected: tuple[str, ...]) -> dict[str, str]:
    if where is None:
        return {}
    if not isinstance(where, dict):
        raise ValueError(
            'where must be a dict of object to SOQL condition, e.g. {"Case": "IsClosed = false"}'
        )
    filters = {}
    for sobject, condition in where.items():
        if sobject not in selected:
            raise ValueError(f"where names {sobject!r}, which is not in objects {list(selected)}")
        if not isinstance(condition, str) or not condition.strip():
            raise ValueError(f"where[{sobject!r}] must be a non-empty SOQL condition")
        filters[sobject] = condition.strip()
    return filters


def _default_resource_name(login: str) -> str:
    """``salesforce_<my domain>``, the same whatever the selection.

    A name that followed the selection would move a narrowed source to a new table and
    state, leaving every document of the old one in the dataset for good; under one name,
    a changed selection forgets what it no longer covers.
    """
    domain = (urlsplit(login).hostname or "").split(".")[0]
    cleaned = re.sub(r"[^a-z0-9_]+", "_", domain.lower()).strip("_") or "org"
    room = _MAX_RESOURCE_NAME - len("salesforce_")
    return f"salesforce_{cleaned[:room].rstrip('_')}"


def _request_floor(objects: tuple[str, ...], comments: bool, tasks: bool, chatter: bool) -> int:
    """The fewest requests one pass over the selection takes: a token, the object list
    (for Chatter), a page per object and per child stream, a deleted-records call each."""
    streams = len(objects) if chatter else 0
    streams += 1 if comments and "Case" in objects else 0
    streams += 1 if tasks else 0
    return 1 + (1 if chatter else 0) + 2 * len(objects) + streams


def _check_resource_name(name: Any) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty name, one per Salesforce org")
    if len(name) > _MAX_RESOURCE_NAME:
        raise ValueError(f"resource_name must be at most {_MAX_RESOURCE_NAME} characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        # state is kept under the name as given while rows go to the normalized table, so
        # two spellings of one name would share a table and keep two states
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")
