"""In-memory Sentry REST API served through ``httpx.MockTransport``.

Recorded against a live Sentry organization on 2026-10-28 (US region, free plan): the
issue and event bodies, the camelCase stack-frame fields, the ``Link`` header, the
rate-limit headers, the 404 body of a deleted issue, the 401 body of a revoked token
and the 200-with-a-different-id answer for a merged-away issue all come from that
recording. Two behaviours are still inferred rather than observed, and the connector is
built so that being wrong about either keeps a document rather than deletes one: an
issue dropping out of the listing once its events age out of retention, and
``events/{slot}/`` answering 404 when no event is left within retention.

What it models:

* ``GET /api/0/organizations/{org}/issues/`` - a JSON array of grouped issues, Link
  header pagination with ``results="true"/"false"``, and the default ``is:unresolved``
  query that an explicit empty ``query=`` turns off.
* ``GET /api/0/organizations/{org}/projects/`` - slug to numeric id, for the
  ``project`` filter.
* ``GET /api/0/issues/{id}/`` - 200 for an issue that exists (even one the listing
  dropped for retention), a redirect-style 200 with a *different* id for a merged-away
  issue, and 404 once a queued deletion has run.
* ``GET /api/0/issues/{id}/events/{recommended,latest,oldest}/`` - an event with an
  ``exception`` entry whose frames carry ``in_app``.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from cognee_community_connector_sentry.sentry import SentryClient, _iter_rows

BASE = "https://sentry.io"
ORG = "mergetober-test"


class FakeSentry:
    def __init__(self, org: str = ORG):
        self.org = org
        self.issues: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.projects: dict[str, int] = {"backend-api": 4505, "frontend": 4506}
        # ids whose DELETE has run: the listing drops them and the GET 404s
        self.deleted: set[str] = set()
        # merged-away id -> surviving id; Sentry answers 200 with the survivor
        self.redirects: dict[str, str] = {}
        # ids dropped from the listing (aged out of retention) that still GET 200
        self.aged_out: set[str] = set()
        self.stuck_cursor = False
        self.calls: list[tuple[str, str, dict]] = []
        self.valid_token = "sntrys_fake"
        self.page_size = 100
        # responses served before the normal ones, e.g. 429s or 500s
        self.queued: list[httpx.Response] = []
        # The live API sends these on *every* response (NOTES, 2026-10-28), so the
        # default is a window with budget left: the header-parsing path then runs in
        # every test instead of only in the ones that set up an exhausted window.
        self.rate_limit: dict[str, str] = {
            "X-Sentry-Rate-Limit-Remaining": "9",
            "X-Sentry-Rate-Limit-Limit": "10",
            "X-Sentry-Rate-Limit-Reset": str(int(time.time()) + 1),
            "X-Sentry-Rate-Limit-ConcurrentRemaining": "24",
            "X-Sentry-Rate-Limit-ConcurrentLimit": "25",
        }
        # when set, those headers go only on answers whose path ends with this, so a
        # test can spend one endpoint's budget and leave the others alone
        self.rate_limit_path: str | None = None
        # {(issue id, slot): event} for the slots that answer with their own event;
        # Sentry serves the same event for every slot on an issue with few events
        self.slot_events: dict[tuple[str, str], dict] = {}
        self._path = ""

    # -- fixtures ------------------------------------------------------------

    def add_issue(
        self,
        issue_id: str,
        *,
        title="ValueError: database is locked",
        project="backend-api",
        level="error",
        status="unresolved",
        substatus="ongoing",
        first_seen=None,
        last_seen=None,
        count="412",
        user_count=37,
        culprit="app/views.py in handle_request",
        assigned_to=None,
        **fields,
    ) -> dict:
        issue = {
            "id": issue_id,
            "shareId": None,
            "shortId": f"BACKEND-API-{issue_id.upper()}",
            "title": title,
            "culprit": culprit,
            "permalink": f"https://{self.org}.sentry.io/issues/{issue_id}/",
            "logger": None,
            "level": level,
            "status": status,
            "substatus": substatus,
            "statusDetails": {},
            "isPublic": False,
            "platform": "python",
            "project": {
                "id": str(self.projects.get(project, 4505)),
                "name": project.replace("-", " ").title(),
                "slug": project,
                "platform": "python",
            },
            "type": "error",
            "metadata": {
                "value": "database is locked",
                "type": "ValueError",
                "filename": "app/views.py",
                "function": "handle_request",
            },
            "numComments": 0,
            "assignedTo": assigned_to,
            "isBookmarked": False,
            "isSubscribed": False,
            "hasSeen": False,
            "isUnhandled": True,
            # Sentry sends both as strings (recorded from the real API, e.g. "5"),
            # not numbers; the renderer must not assume int.
            "count": str(count),
            "userCount": user_count,
            "firstSeen": first_seen or ago(10),
            "lastSeen": last_seen or ago(1),
            "stats": {"24h": [[1761600000, 3]]},
            # recorded from the live API (observations/experiment.json): these move on
            # their own - Sentry re-prioritises and scores issues asynchronously - and
            # must not make the connector re-read or rewrite anything
            "priority": "medium",
            "priorityLockedAt": None,
            "issueType": "error",
            "issueCategory": "error",
            "seerFixabilityScore": None,
            "seerAutofixLastTriggered": None,
            "seerExplorerAutofixLastTriggered": None,
            "subscriptionDetails": None,
            "annotations": [],
            "lifetime": {"count": str(count), "userCount": user_count},
            "filtered": None,
            **fields,
        }
        self.issues[issue_id] = issue
        self.add_event(issue_id)
        return issue

    def add_event(
        self, issue_id: str, *, frames=None, kind="ValueError", value=None, slot=None, event_id=None
    ) -> dict:
        """The event this issue's ``events/{slot}/`` endpoints answer with.

        Without ``slot`` every slot answers with this one event, which is what Sentry
        does for an issue with few events. With ``slot`` only that one does, so a test
        can give the slots distinct events.
        """
        frames = (
            frames
            if frames is not None
            else [
                # The web API serializes frames in camelCase (recorded from the live
                # API: inApp/lineNo/absPath). The last frame keeps the ingest
                # payload's snake_case so both spellings stay covered.
                {
                    "filename": "django/core/handlers/base.py",
                    "absPath": "/usr/lib/python3/django/core/handlers/base.py",
                    "function": "_get_response",
                    "lineNo": 181,
                    "colNo": None,
                    "inApp": False,
                    "module": "django.core.handlers.base",
                },
                {
                    "filename": "app/views.py",
                    "absPath": "/srv/app/views.py",
                    "function": "handle_request",
                    "lineNo": 42,
                    "colNo": None,
                    "inApp": True,
                    "module": "app.views",
                },
                {
                    "filename": "app/db.py",
                    "function": "execute",
                    "lineno": 77,
                    "in_app": True,
                },
            ]
        )
        event = {
            "id": f"ev{issue_id}",
            "groupID": issue_id,
            "eventID": event_id or "f" * 32,
            "dateCreated": ago(1),
            "entries": [
                {
                    "type": "exception",
                    "data": {
                        "values": [
                            {
                                "type": kind,
                                "value": value if value is not None else "database is locked",
                                "module": "builtins",
                                "stacktrace": {"frames": frames},
                            }
                        ]
                    },
                },
                {"type": "breadcrumbs", "data": {"values": [{"message": "noise"}] * 50}},
            ],
            "contexts": {"runtime": {"name": "CPython", "version": "3.12.3"}},
        }
        if event_id:
            event["id"] = event_id
        if slot is None:
            self.events[issue_id] = event
        else:
            self.slot_events[(issue_id, slot)] = event
        return event

    def delete_issue(self, issue_id: str) -> None:
        """What ``DELETE /issues/{id}/`` leaves behind once its queued job has run."""
        self.issues.pop(issue_id, None)
        self.deleted.add(issue_id)

    def merge_into(self, old_id: str, surviving_id: str) -> None:
        """Merge ``old_id`` away: it leaves the listing and GETs answer the survivor."""
        self.issues.pop(old_id, None)
        self.redirects[old_id] = surviving_id

    def age_out(self, issue_id: str) -> None:
        """Past the plan's event retention: out of the listing, still served by id."""
        self.aged_out.add(issue_id)

    # -- transport -----------------------------------------------------------

    def client(self) -> httpx.Client:
        # late binding, so ``serve`` can swap the handler after the client is built
        return httpx.Client(transport=httpx.MockTransport(lambda request: self._handle(request)))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append((request.method, path, params))
        self._path = path
        if self.queued:
            return self.queued.pop(0)
        if request.headers.get("authorization") != f"Bearer {self.valid_token}":
            # Sentry answers a revoked or wrong token with 401, never a 404
            return self._json(401, {"detail": "Invalid token"})
        if path == f"/api/0/organizations/{self.org}/issues/":
            return self._issue_listing(request, params)
        if path == f"/api/0/organizations/{self.org}/projects/":
            records = [
                {"id": str(pid), "slug": slug, "name": slug} for slug, pid in self.projects.items()
            ]
            return self._paged(request, records, params)
        if path.startswith("/api/0/issues/"):
            rest = path[len("/api/0/issues/") :].strip("/").split("/")
            if len(rest) == 1:
                return self._issue_detail(rest[0])
            if len(rest) == 3 and rest[1] == "events":
                return self._event(rest[0], rest[2])
        return self._json(404, {"detail": "The requested resource does not exist"})

    def _issue_listing(self, request: httpx.Request, params: dict) -> httpx.Response:
        # repeated ``project`` params: dict(url.params) would keep only one
        projects = set(request.url.params.get_list("project"))
        issues = [
            issue
            for issue in self.issues.values()
            if issue["id"] not in self.aged_out and self._keep(issue, params, projects)
        ]
        # sort=date is newest last seen first
        issues.sort(key=lambda issue: (issue["lastSeen"], issue["id"]), reverse=True)
        return self._paged(request, issues, params)

    def _keep(self, issue: dict, params: dict, projects: set[str]) -> bool:
        # Sentry's DEFAULT_QUERY: without an explicit query, only unresolved issues
        query = params.get("query", "is:unresolved")
        if "is:unresolved" in query and issue["status"] != "unresolved":
            return False
        if not projects:
            return True
        project = issue.get("project")
        # a row whose project the payload does not describe: the real API always sends
        # one, but the filter is server-side, so such a row is still inside it
        if not isinstance(project, dict) or not project.get("id"):
            return True
        return project["id"] in projects

    def _issue_detail(self, issue_id: str) -> httpx.Response:
        if issue_id in self.redirects:
            # get_group_with_redirect: the surviving issue, under a different id
            survivor = self.issues.get(self.redirects[issue_id])
            if survivor:
                return self._json(200, survivor)
        issue = self.issues.get(issue_id)
        if issue is not None:
            return self._json(200, issue)
        return self._json(404, {"detail": "The requested resource does not exist"})

    def _event(self, issue_id: str, slot: str) -> httpx.Response:
        if slot not in ("recommended", "latest", "oldest"):
            return self._json(404, {"detail": "The requested resource does not exist"})
        event = self.slot_events.get((issue_id, slot)) or self.events.get(issue_id)
        if event is None or issue_id not in self.issues:
            return self._json(404, {"detail": "The requested resource does not exist"})
        return self._json(200, event)

    def _paged(self, request: httpx.Request, records: list, params: dict) -> httpx.Response:
        """One page plus Sentry's Link header, cursor ``0:<offset>:0``."""
        offset = _cursor_offset(params.get("cursor"))
        size = min(int(params.get("limit", 100)), self.page_size)
        page = records[offset : offset + size]
        has_more = len(records) > offset + size
        if self.stuck_cursor:
            # a server that keeps handing back the cursor it was given: the listing
            # would repeat this page for as long as the caller is willing to ask
            offset = _cursor_offset(params.get("cursor"))
            has_more = True
            size = 0
        url = str(request.url.copy_remove_param("cursor"))
        link = ", ".join(
            [
                f'<{url}&cursor=0:{max(offset - size, 0)}:1>; rel="previous"; '
                f'results="{"true" if offset else "false"}"; cursor="0:{max(offset - size, 0)}:1"',
                f'<{url}&cursor=0:{offset + size}:0>; rel="next"; '
                f'results="{"true" if has_more else "false"}"; cursor="0:{offset + size}:0"',
            ]
        )
        return self._json(200, page, headers={"Link": link})

    def _json(self, status: int, body, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(
            status,
            content=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                **(
                    self.rate_limit
                    if self.rate_limit_path is None or self._path.endswith(self.rate_limit_path)
                    else {}
                ),
                **(headers or {}),
            },
        )


def _cursor_offset(cursor: str | None) -> int:
    """Sentry cursors are ``<value>:<offset>:<is_prev>``."""
    if not cursor:
        return 0
    parts = cursor.split(":")
    return int(parts[1]) if len(parts) == 3 else 0


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def client_for(fake: FakeSentry, sleeps: list | None = None) -> SentryClient:
    sleep = sleeps.append if sleeps is not None else (lambda _s: None)
    return SentryClient(fake.valid_token, org_slug=fake.org, http=fake.client(), sleep=sleep)


def sync(
    fake: FakeSentry,
    state: dict,
    sleeps: list | None = None,
    client: SentryClient | None = None,
    **kwargs,
):
    """One run: ``(upserts by id, deleted ids, stats)``."""
    stats: dict = {}
    rows = list(_iter_rows(client or client_for(fake, sleeps), state, stats, **kwargs))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = {row["id"] for row in rows if row["_deleted"]}
    return upserts, deletes, stats


def serve(fake: FakeSentry, path_suffix: str, status: int, body: bytes = b"{}"):
    """Answer requests whose path ends with ``path_suffix``; returns an undo."""
    original = fake._handle

    def handle(request):
        if request.url.path.endswith(path_suffix):
            return httpx.Response(status, content=body)
        return original(request)

    fake._handle = handle
    return lambda: setattr(fake, "_handle", original)


def listing_calls(fake: FakeSentry) -> list[tuple[str, str, dict]]:
    """The organization issue-listing requests made so far."""
    return [call for call in fake.calls if call[1].endswith(f"/organizations/{fake.org}/issues/")]


def event_calls(fake: FakeSentry) -> list[tuple[str, str, dict]]:
    return [call for call in fake.calls if "/events/" in call[1]]
