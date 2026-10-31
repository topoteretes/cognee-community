"""In-memory stand-in for the Discord REST API, shared by the test modules."""

import itertools
import re
from datetime import datetime, timezone

import pytest

from cognee_community_connector_discord.discord import DiscordAPIError, time_snowflake

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
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
        self.thread_errors: dict[str, tuple[int, int]] = {}
        # Discord's last_message_id can keep pointing at a deleted message.
        self.stale_last: dict[str, str] = {}
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

    def _last(self, channel_id):
        ids = [int(m["id"]) for m in self.messages.get(channel_id, [])]
        return self.stale_last.get(channel_id) or (str(max(ids)) if ids else None)

    @staticmethod
    def _fail(errors, key):
        if key in errors:
            status, code = errors[key]
            raise DiscordAPIError(f"HTTP {status}", status=status, code=code)

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
                channel["last_message_id"] = self._last(channel["id"])
            return self.channels
        if path == f"/guilds/{GUILD}/threads/active":
            threads = [t for t in self.threads if not t["thread_metadata"]["archived"]]
            return {"threads": [{**t, "last_message_id": self._last(t["id"])} for t in threads]}
        match = re.fullmatch(r"/channels/(\d+)/threads/archived/(public|private)", path)
        if match:
            # Keyed by channel id, or "<channel id>:private" for the private listing only.
            self._fail(self.thread_errors, match.group(1))
            if match.group(2) == "private":
                self._fail(self.thread_errors, f"{match.group(1)}:private")
            private = match.group(2) == "private"
            threads = [
                {**t, "last_message_id": self._last(t["id"])}
                for t in self.threads
                if t["parent_id"] == match.group(1)
                and t["thread_metadata"]["archived"]
                and (t["type"] == 12) == private
            ]
            return {"threads": threads, "members": [], "has_more": False}
        match = re.fullmatch(r"/channels/(\d+)/messages", path)
        if match:
            channel = match.group(1)
            self._fail(self.errors, channel)
            ordered = sorted(self.messages.get(channel, []), key=lambda m: int(m["id"]))
            limit = int(params["limit"])
            if "after" not in params:
                # Without ``after`` Discord answers with the latest messages.
                return list(reversed(ordered[-limit:]))
            newer = [m for m in ordered if int(m["id"]) > int(params["after"])]
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
