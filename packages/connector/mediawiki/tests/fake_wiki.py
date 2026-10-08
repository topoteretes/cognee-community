"""An in-memory MediaWiki Action API served through ``httpx.MockTransport``.

It implements the slice of ``api.php`` the connector uses (siteinfo, tokens,
login, recentchanges, allpages / categorymembers generators, page lookups by
id or title with info/revisions/categories/extracts, and action=parse) with
``formatversion=2`` response shapes, as observed on a live MediaWiki 1.47.
Lists are paginated in tiny pages so continuation is always exercised.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import httpx

API_URL = "https://wiki.example.org/w/api.php"
PAGE_SIZE = 2  # continuation on every list with more than two entries


def _ts(minutes: int) -> str:
    base = datetime(2026, 10, 1, tzinfo=UTC)
    return (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(title: str) -> str:
    title = title.replace("_", " ").strip()
    if ":" in title:
        prefix, rest = title.split(":", 1)
        return f"{prefix[:1].upper()}{prefix[1:]}:{rest[:1].upper()}{rest[1:]}"
    return title[:1].upper() + title[1:]


@dataclass
class Page:
    pageid: int
    title: str
    ns: int
    text: str
    categories: list[str] = field(default_factory=list)
    hidden_categories: list[str] = field(default_factory=list)
    redirect_to: str | None = None
    contentmodel: str = "wikitext"
    revisions: list[dict] = field(default_factory=list)


class FakeWiki:
    def __init__(self, *, text_extracts: bool = True):
        self.text_extracts = text_extracts
        self.pages: dict[int, Page] = {}
        self.recentchanges: list[dict] = []
        self.minute = 0
        self.next_revid = 100
        self.next_rcid = 1000
        self.requests: list[dict] = []
        self.credentials: tuple[str, str] | None = None
        self.logged_in = False
        # Programmable failures: popped one per request, e.g. [("http", 429)].
        self.failures: list[tuple[str, object]] = []

    # -- wiki editing helpers (each one also writes the feed, like MediaWiki) --
    def tick(self, minutes: int = 1) -> str:
        self.minute += minutes
        return _ts(self.minute)

    def now(self) -> str:
        return _ts(self.minute)

    def _revise(self, page: Page, user: str, comment: str, **extra) -> dict:
        self.next_revid += 1
        revision = {
            "revid": self.next_revid,
            "parentid": page.revisions[0]["revid"] if page.revisions else 0,
            "user": user,
            "timestamp": self.tick(),
            "comment": comment,
            **extra,
        }
        page.revisions.insert(0, revision)
        return revision

    def _feed(self, page: Page, kind: str, timestamp: str, **extra) -> None:
        self.next_rcid += 1
        entry = {
            "type": kind,
            "ns": page.ns,
            "title": page.title,
            "pageid": page.pageid,
            "revid": page.revisions[0]["revid"] if kind != "log" else 0,
            "rcid": self.next_rcid,
            "timestamp": timestamp,
            **extra,
        }
        self.recentchanges.append(entry)

    def create(self, pageid, title, text, *, ns=0, categories=(), feed=True, **kwargs) -> Page:
        page = Page(pageid, normalize(title), ns, text, list(categories), **kwargs)
        self.pages[pageid] = page
        revision = self._revise(page, "Alice", "created")
        if feed:
            self._feed(page, "new", revision["timestamp"])
        return page

    def edit(self, pageid, text=None, *, categories=None, user="Bob", comment="edit", **rev):
        page = self.pages[pageid]
        if text is not None:
            page.text = text
        if categories is not None:
            page.categories = list(categories)
        revision = self._revise(page, user, comment, **rev)
        self._feed(page, "edit", revision["timestamp"])

    def delete(self, pageid) -> None:
        page = self.pages.pop(pageid)
        self._feed(page, "log", self.tick(), logtype="delete", logaction="delete", logparams={})

    def restore(self, page: Page) -> None:
        self.pages[page.pageid] = page
        self._feed(page, "log", self.tick(), logtype="delete", logaction="restore", logparams={})

    def move(self, pageid, new_title, *, new_ns=None, leave_redirect=True, redirect_id=None):
        page = self.pages[pageid]
        old_title, old_ns = page.title, page.ns
        page.title = normalize(new_title)
        page.ns = old_ns if new_ns is None else new_ns
        revision = self._revise(page, "Carol", f"moved from {old_title}")
        self.next_rcid += 1
        self.recentchanges.append(
            {
                "type": "log",
                "ns": old_ns,
                "title": old_title,
                "pageid": pageid,
                "revid": revision["revid"],
                "rcid": self.next_rcid,
                "timestamp": revision["timestamp"],
                "logtype": "move",
                "logaction": "move",
                "logparams": {"target_ns": page.ns, "target_title": page.title},
            }
        )
        if leave_redirect:
            redirect = Page(redirect_id or pageid + 10_000, old_title, old_ns, "")
            redirect.redirect_to = page.title
            self._revise(redirect, "Carol", "redirect")
            self.pages[redirect.pageid] = redirect

    def find(self, title: str) -> Page | None:
        title = normalize(title)
        return next((p for p in self.pages.values() if p.title == title), None)

    # -- the API --------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.method == "POST":
            params.update(parse_qsl(request.content.decode()))
        params["_user_agent"] = request.headers.get("user-agent", "")
        self.requests.append(params)

        if self.failures:
            kind, value = self.failures.pop(0)
            if kind == "http":
                return httpx.Response(value, headers={"Retry-After": "0"})
            if kind == "api":
                return httpx.Response(
                    200, headers={"Retry-After": "0"}, json={"error": {"code": value, "info": ""}}
                )
            if kind == "network":
                raise httpx.ConnectError("boom", request=request)

        action = params.get("action")
        if action == "login":
            return self._json(self._login(params))
        if action == "parse":
            return self._json(self._parse(params))
        if action != "query":
            return self._json({"error": {"code": "badvalue", "info": action}})
        if params.get("meta") == "siteinfo":
            return self._json(
                {
                    "curtimestamp": self.now(),
                    "query": {
                        "general": {"wikiid": "examplewiki", "servername": "wiki.example.org"},
                        "extensions": [{"name": "TextExtracts"}] if self.text_extracts else [],
                    },
                }
            )
        if params.get("meta") == "tokens":
            return self._json({"query": {"tokens": {"logintoken": "tok+\\"}}})
        if params.get("list") == "recentchanges":
            return self._json(self._recentchanges(params))
        return self._json(self._query_pages(params))

    @staticmethod
    def _json(payload: dict) -> httpx.Response:
        return httpx.Response(200, json=payload)

    def _login(self, params: dict) -> dict:
        ok = self.credentials == (params.get("lgname"), params.get("lgpassword"))
        self.logged_in = ok
        if ok and params.get("lgtoken") == "tok+\\":
            return {"login": {"result": "Success", "lgusername": params["lgname"]}}
        return {"login": {"result": "Failed", "reason": "Incorrect username or password."}}

    def _recentchanges(self, params: dict) -> dict:
        types = set((params.get("rctype") or "edit|new|log").split("|"))
        entries = sorted(self.recentchanges, key=lambda e: (e["timestamp"], e["rcid"]))
        if params.get("rcstart"):
            entries = [e for e in entries if e["timestamp"] >= params["rcstart"]]
        entries = [e for e in entries if e["type"] in types]
        limit = 1 if params.get("rclimit") == "1" else PAGE_SIZE
        offset = int(params.get("rccontinue") or 0)
        chunk = entries[offset : offset + limit]
        response = {"query": {"recentchanges": chunk}}
        if offset + limit < len(entries) and params.get("rclimit") != "1":
            response["continue"] = {"rccontinue": str(offset + limit), "continue": "-||"}
        return response

    def _select(self, params: dict) -> tuple[list[dict], dict | None]:
        """Resolve the page set of a query; return (page stubs, continue)."""
        generator = params.get("generator")
        if generator in ("allpages", "categorymembers"):
            if generator == "allpages":
                candidates = [
                    p
                    for p in self.pages.values()
                    if p.ns == int(params["gapnamespace"])
                    and not (params.get("gapfilterredir") == "nonredirects" and p.redirect_to)
                ]
                key = "gapcontinue"
            else:
                category = normalize(params["gcmtitle"]).split(":", 1)[1]
                candidates = [
                    p for p in self.pages.values() if category in p.categories + p.hidden_categories
                ]
                key = "gcmcontinue"
            candidates.sort(key=lambda p: p.pageid)
            offset = int(params.get(key) or 0)
            chunk = candidates[offset : offset + PAGE_SIZE]
            cont = (
                {key: str(offset + PAGE_SIZE), "continue": "gapcontinue||"}
                if offset + PAGE_SIZE < len(candidates)
                else None
            )
            return [{"page": p} for p in chunk], cont
        if params.get("pageids"):
            stubs = []
            for raw in params["pageids"].split("|"):
                page = self.pages.get(int(raw))
                stubs.append({"page": page} if page else {"pageid": int(raw), "missing": True})
            return stubs, None
        stubs = []
        for raw in params.get("titles", "").split("|"):
            title = normalize(raw)
            page = self.find(title)
            if page and page.redirect_to and params.get("redirects"):
                page = self.find(page.redirect_to)
            if page:
                stubs.append({"page": page})
            else:
                ns = 14 if title.startswith("Category:") else 0
                stubs.append({"ns": ns, "title": title, "missing": True})
        return stubs, None

    def _query_pages(self, params: dict) -> dict:
        stubs, cont = self._select(params)
        props = set((params.get("prop") or "").split("|")) - {""}
        pages = []
        for stub in stubs:
            page: Page | None = stub.get("page")
            if page is None:
                pages.append({k: v for k, v in stub.items() if k != "page"})
                continue
            out = {"pageid": page.pageid, "ns": page.ns, "title": page.title}
            if "info" in props:
                out.update(
                    contentmodel=page.contentmodel,
                    lastrevid=page.revisions[0]["revid"],
                    fullurl=f"https://wiki.example.org/wiki/{page.title.replace(' ', '_')}",
                )
                if page.redirect_to:
                    out["redirect"] = True
            if "revisions" in props:
                limit = int(params.get("rvlimit") or 1)
                out["revisions"] = [dict(r) for r in page.revisions[:limit]]
            if "categories" in props:
                cats = [{"ns": 14, "title": f"Category:{c}"} for c in page.categories]
                cats += [
                    {"ns": 14, "title": f"Category:{c}", "hidden": True}
                    for c in page.hidden_categories
                ]
                if cats:
                    out["categories"] = cats
            if "extracts" in props:
                out["extract"] = page.text
            pages.append(out)
        response: dict = {"query": {"pages": pages}}
        if cont:
            response["continue"] = cont
        return response

    def _parse(self, params: dict) -> dict:
        page = self.pages.get(int(params["pageid"]))
        if page is None:
            return {"error": {"code": "nosuchpageid", "info": "There is no page with ID."}}
        body = "".join(f"<p>{html.escape(line)}</p>" for line in page.text.splitlines())
        markup = (
            '<div class="mw-parser-output"><style>.x{color:red}</style>'
            f'<h2>Overview<span class="mw-editsection">[edit]</span></h2>{body}'
            '<sup class="reference">[1]</sup></div>'
        )
        return {"parse": {"title": page.title, "pageid": page.pageid, "text": markup}}
