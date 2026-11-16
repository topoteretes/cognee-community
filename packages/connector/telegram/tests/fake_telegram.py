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
    """In-memory Bot API: a list of updates served like getUpdates does.

    Like the real API, a getUpdates call with an offset confirms (drops) every
    update below it, so a test can see an update get lost for good.
    """

    def __init__(self, updates=None, conflict=False, bot_id=99, can_read_all=True):
        self.updates = list(updates or [])
        self.conflict = conflict
        self.bot_id = bot_id
        self.can_read_all = can_read_all
        self.calls = []
        self.next_id = 1000

    def add(self, **update):
        # Ids keep counting after confirmed updates are dropped, like Telegram's.
        self.updates.append({"update_id": self.next_id, **update})
        self.next_id += 1

    def add_with_id(self, update_id, **update):
        self.updates.append({"update_id": update_id, **update})

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getMe":
            return {
                "id": self.bot_id,
                "is_bot": True,
                "username": "test_bot",
                "can_read_all_group_messages": self.can_read_all,
            }
        if method == "getUpdates":
            if self.conflict:
                raise TelegramWebhookConflictError("409")
            offset = params.get("offset")
            if offset is not None:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
            return self.updates[: params.get("limit", 100)]
        raise AssertionError(method)

    def get_updates_calls(self):
        return [p for m, p in self.calls if m == "getUpdates"]
