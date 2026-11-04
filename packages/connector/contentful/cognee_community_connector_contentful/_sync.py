"""Disk-backed event reconciliation; content payloads do not accumulate in memory."""

import json
import sqlite3
from datetime import datetime
from tempfile import TemporaryDirectory

from ._http import fingerprint
from .exceptions import ContentfulResponseError


def _timestamp(item):
    system = item["sys"]
    value = system.get("deletedAt") if system["type"].startswith("Deleted") else None
    value = value or system.get("updatedAt")
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise ContentfulResponseError("Contentful returned an invalid event timestamp.") from None


class EventSpool:
    """Keep payloads in a private temporary database, with a bounded SQLite cache."""

    def __enter__(self):
        self.directory = TemporaryDirectory(prefix="cognee-contentful-")
        self.connection = sqlite3.connect(self.directory.name + "/events.sqlite")
        self.connection.execute("PRAGMA cache_size=-1024")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute(
            "CREATE TABLE events (kind TEXT, id TEXT, hash TEXT, payload TEXT, "
            "PRIMARY KEY(kind, id, hash))"
        )
        return self

    def __exit__(self, *_args):
        self.connection.close()
        self.directory.cleanup()

    def clear(self):
        self.connection.execute("DELETE FROM events")

    def add(self, item):
        system = item["sys"]
        kind, upstream_id = system["type"].removeprefix("Deleted"), system["id"]
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
            (kind, upstream_id, fingerprint(item), json.dumps(item, ensure_ascii=False)),
        )
        if not cursor.rowcount:
            return False
        count = self.connection.execute(
            "SELECT COUNT(*) FROM events WHERE kind=? AND id=?", (kind, upstream_id)
        ).fetchone()[0]
        return count == 1

    def contains(self, kind, upstream_id, item):
        return (
            self.connection.execute(
                "SELECT 1 FROM events WHERE kind=? AND id=? AND hash=?",
                (kind, upstream_id, fingerprint(item)),
            ).fetchone()
            is not None
        )

    def identities(self):
        return self.connection.execute(
            "SELECT kind, id, COUNT(*) FROM events GROUP BY kind, id ORDER BY kind, id"
        )

    def items(self, kind, upstream_id):
        cursor = self.connection.execute(
            "SELECT payload FROM events WHERE kind=? AND id=? ORDER BY hash", (kind, upstream_id)
        )
        return (json.loads(row[0]) for row in cursor)


def resolve_event(client, spool, kind, upstream_id, count):
    items = spool.items(kind, upstream_id)
    first = next(items)
    if count == 1:
        return first
    latest, latest_stamp, unique, missing_stamp = first, _timestamp(first), True, False
    for item in items:
        stamp = _timestamp(item)
        if stamp is None:
            missing_stamp = True
        elif latest_stamp is None or stamp > latest_stamp:
            latest, latest_stamp, unique = item, stamp, True
        elif stamp == latest_stamp:
            unique = False
    # A missing timestamp cannot establish ordering, even if another event has one.
    missing_stamp = missing_stamp or _timestamp(first) is None
    endpoint = "entries" if kind == "Entry" else "assets"
    for _attempt in range(2):
        current = client.get(f"{endpoint}/{upstream_id}", {"locale": "*"}, missing=True)
        if current is None:
            if unique and not missing_stamp and latest["sys"]["type"].startswith("Deleted"):
                return latest
            continue
        client.validate_item(current, {kind})
        if current["sys"]["id"] != upstream_id:
            raise ContentfulResponseError(
                "Contentful conflict lookup returned a different resource."
            )
        stamp = _timestamp(current)
        if stamp is not None and (latest_stamp is None or stamp > latest_stamp):
            return current
        if stamp == latest_stamp and spool.contains(kind, upstream_id, current):
            return current
    if unique and not missing_stamp:
        # The CDN may lag Sync. Never replace known newer Sync evidence with its cache.
        return latest
    raise ContentfulResponseError("Contentful conflict lookup cannot resolve the Sync evidence.")
