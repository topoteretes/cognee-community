"""In-memory Microsoft Graph stand-in for the Teams connector tests.

``FakeGraph`` is mounted on a real ``requests.Session`` as a transport
adapter, so the connector's whole HTTP stack runs unchanged: dlt's
``OAuth2ClientCredentials`` token request, ``rest_api`` parent/child
resolution, ``JSONLinkPaginator`` following ``@odata.nextLink``, and the
``response_actions`` hooks. Only the network is replaced.

It mirrors the Graph behaviour the connector relies on:

* channel messages are sorted by the last modified time of the whole reply
  chain, newest first, and paged by ``$top``;
* ``$expand=replies`` inlines at most ``inline_reply_limit`` replies and links
  the rest through ``replies@odata.nextLink``;
* deleted messages stay in the listing with ``deletedDateTime`` set and an
  empty body.
"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import requests
from requests.adapters import BaseAdapter

GRAPH_HOST = "graph.microsoft.com"
LOGIN_HOST = "login.microsoftonline.com"
GRAPH_ROOT = f"https://{GRAPH_HOST}/v1.0"


class FakeGraph(BaseAdapter):
    """Serves the handful of Graph endpoints the Teams connector reads."""

    def __init__(self, inline_reply_limit: int = 1000, page_size_cap: int = 50):
        super().__init__()
        self.inline_reply_limit = inline_reply_limit
        self.page_size_cap = page_size_cap
        self.teams: dict[str, dict] = {}
        # (team_id, channel_id) -> {root_id: root message with "replies" list}
        self.threads: dict[tuple[str, str], dict[str, dict]] = {}
        self.requests: list[tuple[str, str]] = []
        self.token_requests = 0
        # Serve a nextLink that points back at the same page, as a buggy server might.
        self.repeat_next_link = False
        # Answer the token request with this status instead of a token.
        self.token_status = 200
        # (path substring, status, headers): served once instead of the real
        # response, to simulate throttling or outages.
        self.failures: list[tuple[str, int, dict]] = []
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)
        self._next_id = 1000

    # -- fixture builders --------------------------------------------------

    def session(self) -> requests.Session:
        """A plain session routed to this fake (no retries)."""
        session = requests.Session()
        self.mount_on(session)
        return session

    def mount_on(self, session: requests.Session) -> None:
        session.mount(f"https://{GRAPH_HOST}/", self)
        session.mount(f"https://{LOGIN_HOST}/", self)

    def add_team(self, team_id: str, name: str = "") -> None:
        self.teams[team_id] = {"id": team_id, "displayName": name or team_id, "channels": {}}

    def add_channel(self, team_id: str, channel_id: str, name: str, membership="standard") -> None:
        self.teams[team_id]["channels"][channel_id] = {
            "id": channel_id,
            "displayName": name,
            "membershipType": membership,
        }
        self.threads.setdefault((team_id, channel_id), {})

    def remove_channel(self, team_id: str, channel_id: str) -> None:
        self.teams[team_id]["channels"].pop(channel_id)
        self.threads.pop((team_id, channel_id), None)

    def post(self, team_id, channel_id, text, author="Robin Kline", subject=None, html=True):
        """Post a new root message; returns its id."""
        message = self._message(text, author, html)
        message["subject"] = subject
        message["replies"] = []
        message["webUrl"] = f"https://teams.microsoft.com/l/message/{channel_id}/{message['id']}"
        self.threads[(team_id, channel_id)][message["id"]] = message
        return message["id"]

    def reply(self, team_id, channel_id, root_id, text, author="Jane Smith", html=True):
        """Reply to a thread; returns the reply id."""
        reply = self._message(text, author, html)
        reply["replyToId"] = root_id
        self.threads[(team_id, channel_id)][root_id]["replies"].append(reply)
        return reply["id"]

    def edit(self, team_id, channel_id, message_id, text):
        message = self._find(team_id, channel_id, message_id)
        message["body"]["content"] = text
        now = self._tick()
        message["lastModifiedDateTime"] = now
        message["lastEditedDateTime"] = now

    def delete(self, team_id, channel_id, message_id):
        message = self._find(team_id, channel_id, message_id)
        message["body"]["content"] = ""
        now = self._tick()
        message["lastModifiedDateTime"] = now
        message["deletedDateTime"] = now

    def system_event(self, team_id, channel_id):
        message = self._message("<systemEventMessage/>", None, html=True)
        message["messageType"] = "systemEventMessage"
        message["replies"] = []
        self.threads[(team_id, channel_id)][message["id"]] = message
        return message["id"]

    def fail_once(self, path_fragment: str, status: int, headers: dict | None = None) -> None:
        self.failures.append((path_fragment, status, headers or {}))

    def message_requests(self) -> list[str]:
        return [path for _method, path in self.requests if path.endswith("/messages")]

    # -- transport ---------------------------------------------------------

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}

        if url.hostname == LOGIN_HOST:
            self.token_requests += 1
            if self.token_status != 200:
                return self._json(request, self.token_status, {"error": "invalid_client"})
            body = parse_qs(request.body if isinstance(request.body, str) else "")
            assert body.get("grant_type") == ["client_credentials"]
            assert body.get("scope") == ["https://graph.microsoft.com/.default"]
            return self._json(request, 200, {"access_token": "fake-token", "expires_in": 3600})

        assert request.headers.get("Authorization") == "Bearer fake-token"
        path = unquote(url.path).removeprefix("/v1.0")
        self.requests.append((request.method, path))

        for index, (fragment, status, headers) in enumerate(self.failures):
            if fragment in path:
                del self.failures[index]
                return self._json(request, status, {"error": {"code": str(status)}}, headers)

        parts = path.strip("/").split("/")
        if parts == ["teams"]:
            teams = [{"id": t["id"], "displayName": t["displayName"]} for t in self.teams.values()]
            return self._json(request, 200, {"value": teams})
        if len(parts) == 3 and parts[0] == "teams" and parts[2] == "channels":
            return self._channels(request, parts[1], query)
        if len(parts) == 5 and parts[2] == "channels" and parts[4] == "messages":
            return self._messages(request, parts[1], parts[3], query)
        if len(parts) == 7 and parts[4] == "messages" and parts[6] == "replies":
            return self._replies(request, parts[1], parts[3], parts[5], query)
        return self._json(request, 404, {"error": {"code": "NotFound"}})

    def close(self):
        pass

    # -- endpoints ---------------------------------------------------------

    def _channels(self, request, team_id, query):
        team = self.teams.get(team_id)
        if team is None:
            return self._json(request, 404, {"error": {"code": "NotFound"}})
        channels = list(team["channels"].values())
        if query.get("$filter") == "membershipType eq 'standard'":
            channels = [c for c in channels if c["membershipType"] == "standard"]
        return self._json(request, 200, {"value": [dict(c) for c in channels]})

    def _messages(self, request, team_id, channel_id, query):
        threads = self.threads.get((team_id, channel_id))
        if threads is None:
            return self._json(request, 404, {"error": {"code": "NotFound"}})
        assert query.get("$expand") == "replies"
        top = int(query.get("$top", 20))
        assert top <= self.page_size_cap, "Graph caps channel message pages at 50"
        offset = int(query.get("$skiptoken", 0))

        ordered = sorted(threads.values(), key=_chain_modified, reverse=True)
        page = ordered[offset : offset + top]
        body = {"value": [self._root_view(team_id, channel_id, root) for root in page]}
        if self.repeat_next_link and ordered:
            body["@odata.nextLink"] = request.url
        elif offset + top < len(ordered):
            params = urlencode({"$top": top, "$expand": "replies", "$skiptoken": offset + top})
            body["@odata.nextLink"] = (
                f"{GRAPH_ROOT}/teams/{team_id}/channels/{channel_id}/messages?{params}"
            )
        return self._json(request, 200, body)

    def _replies(self, request, team_id, channel_id, root_id, query):
        root = self.threads[(team_id, channel_id)][root_id]
        offset = int(query.get("$skiptoken", 0))
        newest_first = sorted(root["replies"], key=lambda r: r["createdDateTime"], reverse=True)
        page = newest_first[offset : offset + self.inline_reply_limit]
        body = {"value": [dict(reply) for reply in page]}
        if offset + self.inline_reply_limit < len(newest_first):
            body["@odata.nextLink"] = self._replies_link(
                team_id, channel_id, root_id, offset + self.inline_reply_limit
            )
        return self._json(request, 200, body)

    def _root_view(self, team_id, channel_id, root):
        view = {key: value for key, value in root.items() if key != "replies"}
        newest_first = sorted(root["replies"], key=lambda r: r["createdDateTime"], reverse=True)
        view["replies"] = [dict(reply) for reply in newest_first[: self.inline_reply_limit]]
        if len(newest_first) > self.inline_reply_limit:
            view["replies@odata.nextLink"] = self._replies_link(
                team_id, channel_id, root["id"], self.inline_reply_limit
            )
        return view

    def _replies_link(self, team_id, channel_id, root_id, offset):
        return (
            f"{GRAPH_ROOT}/teams/{team_id}/channels/{channel_id}/messages/{root_id}"
            f"/replies?$skiptoken={offset}"
        )

    # -- helpers -----------------------------------------------------------

    def _message(self, text, author, html):
        self._next_id += 1
        now = self._tick()
        return {
            "id": str(self._next_id),
            "replyToId": None,
            "messageType": "message",
            "createdDateTime": now,
            "lastModifiedDateTime": now,
            "lastEditedDateTime": None,
            "deletedDateTime": None,
            "subject": None,
            "from": {"user": {"displayName": author}} if author else None,
            "body": {"contentType": "html" if html else "text", "content": text},
            "attachments": [],
            "mentions": [],
        }

    def _find(self, team_id, channel_id, message_id):
        for root in self.threads[(team_id, channel_id)].values():
            if root["id"] == message_id:
                return root
            for reply in root["replies"]:
                if reply["id"] == message_id:
                    return reply
        raise KeyError(message_id)

    def _tick(self) -> str:
        # Advance well past the connector's cursor overlap so every change is
        # unambiguously newer than the previous sync.
        self._clock += timedelta(hours=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _json(request, status, payload, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        response.headers.update(headers or {})
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response


def _chain_modified(root: dict) -> str:
    """Graph's sort key: the latest change across the root and its replies."""
    stamps = [root["lastModifiedDateTime"]]
    stamps.extend(reply["lastModifiedDateTime"] for reply in root["replies"])
    return max(stamps)
