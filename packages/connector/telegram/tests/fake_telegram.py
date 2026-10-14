"""An in-memory Telegram Bot API for tests (no token, no network).

The payloads mirror what a real test bot received: groups, a supergroup upgrade,
a channel, edits and the bot being removed from a chat.
"""

from cognee_community_connector_telegram.telegram import TelegramWebhookConflictError

GROUP = {"id": -100, "type": "group", "title": "Team chat"}
SUPERGROUP = {"id": -1009, "type": "supergroup", "title": "Team chat"}
CHANNEL = {"id": -1001, "type": "channel", "title": "News", "username": "news_chan"}
OTHER = {"id": -555, "type": "group", "title": "Unrelated"}
ALICE = {"id": 1, "is_bot": False, "first_name": "Alice", "username": "alice"}
BOB = {"id": 2, "is_bot": False, "first_name": "Bob"}


def msg(chat, message_id, text=None, sender=ALICE, date=1_760_000_000, **extra):
    m = {"message_id": message_id, "chat": chat, "date": date, **extra}
    if text is not None:
        m["text"] = text
    if sender is not None and chat.get("type") != "channel":
        m["from"] = sender
    return m


def removed(chat, status="left"):
    return {"chat": chat, "new_chat_member": {"status": status, "user": {"id": 99}}}


class FakeBot:
    """In-memory Bot API: a list of updates served like getUpdates does."""

    def __init__(self, updates=None, conflict=False):
        self.updates = list(updates or [])
        self.conflict = conflict
        self.calls = []

    def add(self, **update):
        next_id = (self.updates[-1]["update_id"] + 1) if self.updates else 1000
        self.updates.append({"update_id": next_id, **update})

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getMe":
            return {"id": 99, "is_bot": True, "username": "test_bot"}
        if method == "getUpdates":
            if self.conflict:
                raise TelegramWebhookConflictError("409")
            offset = params.get("offset")
            pending = [u for u in self.updates if offset is None or u["update_id"] >= offset]
            return pending[: params.get("limit", 100)]
        raise AssertionError(method)
