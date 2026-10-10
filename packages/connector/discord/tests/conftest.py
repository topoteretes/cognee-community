"""In-memory stand-in for the Discord REST API, shared by the test modules."""

import itertools
import re
from datetime import UTC, datetime

import pytest

from cognee_community_connector_discord.discord import DiscordAPIError, time_snowflake

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
GUILD = "900"
GENERAL = "901"
FORUM = "902"
USER = {"id": "11", "username": "priya", "global_name": "Priya Shah"}
SAM = {"id": "12", "username": "sam", "global_name": "Sam Lee"}
BOT = {"id": "13", "username": "ci-bot", "bot": True}


class FakeDiscord:
    """Implements ``get`` like Discord: messages newest first, paged with ``after``."""

    def __init__(self):
        self.flags = 1 << 19
        self.channels = [
            {"id": GENERAL, "type": 0, "name": "general"},
            {
                "id": FORUM,
                "type": 15,
                "name": "ideas",
                "available_tags": [{"id": "71", "name": "export"}, {"id": "72", "name": "ui"}],
            },
            {"id": "903", "type": 2, "name": "voice"},
        ]
        self.threads: list[dict] = []
        self.messages: dict[str, list[dict]] = {GENERAL: []}
        self.errors: dict[str, tuple[int, int]] = {}
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(1)

    def message(self, channel, when, text, author=USER, **extra):
        message = {
            "id": str(time_snowflake(when) + next(self._ids)),
            "channel_id": channel,
            "type": 0,
            "author": author,
            "content": text,
            "mentions": [],
            "attachments": [],
            **extra,
        }
        self.messages.setdefault(channel, []).append(message)
        return message

    def thread(self, thread_id, parent, name, *, archived=False, type_=11, tags=()):
        self.threads.append(
            {
                "id": thread_id,
                "parent_id": parent,
                "type": type_,
                "name": name,
                "applied_tags": list(tags),
                "thread_metadata": {
                    "archived": archived,
                    "archive_timestamp": "2026-10-01T00:00:00+00:00",
                },
            }
        )
        self.messages.setdefault(thread_id, [])

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "/applications/@me":
            return {"id": "1", "flags": self.flags}
        if path == f"/guilds/{GUILD}":
            return {"id": GUILD, "name": "Acme"}
        if path == f"/guilds/{GUILD}/roles":
            return [{"id": "50", "name": "maintainers"}]
        if path == f"/guilds/{GUILD}/channels":
            for channel in self.channels:
                ids = [int(m["id"]) for m in self.messages.get(channel["id"], [])]
                channel["last_message_id"] = str(max(ids)) if ids else None
            return self.channels
        if path == f"/guilds/{GUILD}/threads/active":
            return {"threads": [t for t in self.threads if not t["thread_metadata"]["archived"]]}
        match = re.fullmatch(r"/channels/(\d+)/threads/archived/(public|private)", path)
        if match:
            private = match.group(2) == "private"
            threads = [
                t
                for t in self.threads
                if t["parent_id"] == match.group(1)
                and t["thread_metadata"]["archived"]
                and (t["type"] == 12) == private
            ]
            return {"threads": threads, "members": [], "has_more": False}
        match = re.fullmatch(r"/channels/(\d+)/messages", path)
        if match:
            channel = match.group(1)
            if channel in self.errors:
                status, code = self.errors[channel]
                raise DiscordAPIError(f"HTTP {status}", status=status, code=code)
            after, limit = int(params["after"]), int(params["limit"])
            newer = sorted(
                (m for m in self.messages.get(channel, []) if int(m["id"]) > after),
                key=lambda m: int(m["id"]),
            )
            return list(reversed(newer[:limit]))
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture
def discord():
    return FakeDiscord()


@pytest.fixture
def fixed_now(monkeypatch):
    from cognee_community_connector_discord import discord as discord_module

    monkeypatch.setattr(discord_module, "_utcnow", lambda: NOW)
    return NOW
