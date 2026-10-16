"""Opt-in live smoke test against a real Evernote account.

Skipped unless ``EVERNOTE_LIVE=1`` **and** a token is available, so the default
suite stays offline. Run it with::

    EVERNOTE_LIVE=1 uv run pytest tests/test_evernote_live.py -q -s

Requires real Evernote credentials (see the README — API keys are gated), which is
why it is not part of the normal run.

What it proves that the offline suite cannot: that the vendored EDAM Thrift stubs
and the OAuth 1.0a token are accepted by Evernote's servers, and that the USN
sync-chunk protocol behaves as documented against a live account.
"""

import os

import pytest

from cognee_community_connector_evernote.evernote import (
    EvernoteNoteStore,
    _EvernoteConfig,
    _render_enml,
    resolve_auth_token,
    sync_notes,
    token_file_path,
)

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("EVERNOTE_LIVE"),
        reason="set EVERNOTE_LIVE=1 to run tests against a live Evernote account",
    ),
    pytest.mark.skipif(
        not (os.environ.get("EVERNOTE_AUTH_TOKEN") or os.path.exists(token_file_path())),
        reason="set EVERNOTE_AUTH_TOKEN or run examples/authorize.py first",
    ),
]


@pytest.fixture(scope="module")
def store():
    token = resolve_auth_token()
    return EvernoteNoteStore(
        token,
        sandbox=os.environ.get("EVERNOTE_SANDBOX", "").lower() in ("1", "true", "yes"),
    )


def test_live_sync_state_and_notebooks(store):
    state = store.get_sync_state()
    assert isinstance(state, int) and state >= 0

    notebooks = store.list_notebooks()
    assert isinstance(notebooks, dict)
    print(f"\n  account USN: {state}")
    print(f"  notebooks:   {len(notebooks)}")
    for guid, name in list(notebooks.items())[:10]:
        print(f"    - {name} ({guid})")


def test_live_sync_produces_rows_and_advances_the_cursor(store):
    """A full scan must yield rows (if the account has notes) and record a cursor."""
    state = {}
    rows = list(sync_notes(store, _EvernoteConfig(chunk_size=10), state))

    assert "cursor_usn" in state and state["scope_key"], "cursor must be recorded"
    print(f"\n  rows yielded: {len(rows)}")
    for row in rows[:5]:
        if row.get("_deleted"):
            print(f"    - tombstone {row['id']}")
        else:
            print(f"    - {row['title'][:50]!r} ({len(row['content'])} chars)")

    # A second run with no changes must be a no-op: this is the incremental
    # guarantee, verified against the live server rather than a fake.
    second = list(sync_notes(store, _EvernoteConfig(chunk_size=10), state))
    assert second == [], f"an idle re-sync must yield nothing, got {len(second)} row(s)"


def test_live_note_content_roundtrip(store):
    """getNoteContent must return renderable ENML for at least one note."""
    rows = [
        row
        for row in sync_notes(store, _EvernoteConfig(chunk_size=10), {})
        if not row.get("_deleted") and row.get("content")
    ]
    if not rows:
        pytest.skip("account has no notes to render")

    assert all(isinstance(row["content"], str) for row in rows)
    assert _render_enml("<div>hello</div>") == "hello", "renderer sanity"
    print(f"\n  rendered {len(rows)} note(s)")
