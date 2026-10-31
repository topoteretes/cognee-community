"""Review regressions: bounded, fake-only tests (no live API credentials)."""

from datetime import datetime, timezone

import pytest
from dlt.pipeline.exceptions import PipelineStepFailed

from tests.test_help_scout import (
    FakeHelpScout,
    FakeResponse,
    _api,
    _article,
    _conv,
    _docs_run,
    _rows,
    _run,
    _state,
    hs,
)


def test_delta_delete_shifting_pages_retries_and_captures_last_edit(tmp_path):
    fake = FakeHelpScout([_conv(i) for i in range(1, 6)])
    _run(tmp_path, fake)
    for conv in fake.conversations[2:]:
        conv["_modified"] = "2026-10-20T11:59:00Z"
        conv["_threads"][0]["body"] = "Edited"

    def delete_on_second_page(base, query):
        if query.get("sortField") == "modifiedAt" and query.get("page") == "2":
            fake.on_page = None
            fake.conversations = [c for c in fake.conversations if c["id"] != 3]

    fake.on_page = delete_on_second_page
    pipeline, _ = _run(tmp_path, fake)
    assert "Edited" in _rows(pipeline)["conversation:5"]
    assert "conversation:3" not in _rows(pipeline)


def test_moving_listing_never_advances_cursor(tmp_path):
    fake = FakeHelpScout([_conv(i) for i in range(1, 5)])
    pipeline, _ = _run(tmp_path, fake)
    before = _state(pipeline)["modified_since"]
    real = fake._hal

    def moving(base, query, key, items):
        response = real(base, query, key, items)
        if query.get("sortField") == "modifiedAt":
            response._payload["page"]["totalElements"] += 1
        return response

    fake._hal = moving
    # Make a new server clock visibly later than the persisted cursor.
    real_get = fake.get

    def later(url, params=None, headers=None):
        response = real_get(url, params, headers)
        response.headers["Date"] = "Wed, 21 Oct 2026 12:00:00 GMT"
        return response

    fake.get = later
    pipeline, _ = _run(tmp_path, fake)
    assert _state(pipeline)["modified_since"] == before


def test_bulk_delete_completes_in_bounded_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "MAX_VERIFY", 3)
    fake = FakeHelpScout([_conv(i) for i in range(1, 8)])
    _run(tmp_path, fake, reconcile_every=3)
    fake.conversations = fake.conversations[-2:]
    # First two quiet runs defer the regular reconcile.
    _run(tmp_path, fake, reconcile_every=3)
    _run(tmp_path, fake, reconcile_every=3)
    pipeline, _ = _run(tmp_path, fake, reconcile_every=3)
    assert len(_rows(pipeline)) == 4
    pipeline, _ = _run(tmp_path, fake, reconcile_every=3)
    assert len(_rows(pipeline)) == 2
    assert not _state(pipeline)["reconcile_pending"]


def test_live_missing_ids_do_not_starve_later_deletions(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "MAX_VERIFY", 2)
    fake = FakeHelpScout([_conv(i) for i in range(1, 6)])
    _run(tmp_path, fake)
    fake.conversations = fake.conversations[:3]
    real = fake._current

    def hidden(base, query):
        return [] if query.get("sortField") == "number" else real(base, query)

    fake._current = hidden
    for _ in range(3):
        pipeline, _ = _run(tmp_path, fake)
    assert set(_rows(pipeline)) == {"conversation:1", "conversation:2", "conversation:3"}
    assert not _state(pipeline)["reconcile_pending"]


def test_rate_stop_remembers_loaded_ids_and_later_deletion(tmp_path):
    fake = FakeHelpScout([_conv(i) for i in range(1, 5)])
    real = fake.get

    def limited(url, params=None, headers=None):
        if "page=2" in url:
            return FakeResponse(429, headers={"X-RateLimit-Retry-After": "900"})
        return real(url, params, headers)

    fake.get = limited
    pipeline, _ = _run(tmp_path, fake)
    assert _state(pipeline)["known_ids"] == [1, 2]
    fake.get = real
    fake.conversations = fake.conversations[1:]
    pipeline, source = _run(tmp_path, fake)
    assert "conversation:1" not in _rows(pipeline)
    assert source.cognee_sync_stats["conversations_forgotten"] == 1


def test_scope_change_stays_pending_after_untrusted_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "MAX_VERIFY", 1)
    fake = FakeHelpScout([_conv(1, mailboxId=1), _conv(2, mailboxId=2), _conv(3, mailboxId=2)])
    _run(tmp_path, fake, mailbox_ids=[1, 2])
    real_hal = fake._hal

    def untrusted(base, query, key, items):
        response = real_hal(base, query, key, items)
        response._payload["page"]["totalElements"] += 1
        return response

    fake._hal = untrusted
    pipeline, _ = _run(tmp_path, fake, mailbox_ids=[1])
    assert _state(pipeline)["config"]["mailbox_ids"] == [1, 2]
    assert _state(pipeline)["reconcile_pending"]
    fake._hal = real_hal
    pipeline, _ = _run(tmp_path, fake, mailbox_ids=[1])
    assert len(_rows(pipeline)) == 2
    assert _state(pipeline)["config"]["mailbox_ids"] == [1, 2]
    pipeline, _ = _run(tmp_path, fake, mailbox_ids=[1])
    assert set(_rows(pipeline)) == {"conversation:1"}
    assert _state(pipeline)["config"]["mailbox_ids"] == [1]


def test_resource_403_keeps_document_but_does_not_break_reconcile(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2)])
    _run(tmp_path, fake)
    fake.conversations = [fake.conversations[0]]
    real = fake._inbox

    def forbidden(base, query):
        if base.endswith("/conversations/2"):
            return FakeResponse(403)
        return real(base, query)

    fake._inbox = forbidden
    pipeline, source = _run(tmp_path, fake)
    assert "conversation:2" in _rows(pipeline)
    assert source.cognee_sync_stats["conversations_access_denied"] == 1
    fake.fail_with = 403
    with pytest.raises(PipelineStepFailed):
        _run(tmp_path, fake)


def test_naive_date_header_is_utc_not_local(monkeypatch):
    # Run even on a UTC host: inspect the parsed timezone semantics directly.
    import os
    import time

    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        api = _api(FakeHelpScout())
        api._note_server_time({"Date": "Tue, 20 Oct 2026 12:00:00 -0000"})
        assert api.server_time == datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    finally:
        if old is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old)
        time.tzset()


def test_missing_date_uses_start_not_end_clock(monkeypatch):
    class Clock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return datetime(2026, 10, 20, 12 if cls.calls == 1 else 14, tzinfo=timezone.utc)

    monkeypatch.setattr(hs, "datetime", Clock)
    fake = FakeHelpScout([_conv(1)])
    real = fake.get

    def no_date(url, params=None, headers=None):
        response = real(url, params, headers)
        response.headers = {}
        return response

    fake.get = no_date
    state = {}
    list(hs._sync_conversations(_api(fake), state, hs._Options(), {}))
    assert state["modified_since"] == "2026-10-20T11:55:00Z"
    assert Clock.calls == 1


def test_404_is_actionable_without_sensitive_url():
    fake = FakeHelpScout()
    with pytest.raises(hs._NotFoundError, match=r"conversations.*404") as info:
        _api(fake).get(f"{hs.MAILBOX_API}/conversations/999999")
    assert "999999" not in str(info.value)
    assert "https://" not in str(info.value)


def test_tags_have_lexical_order_not_set_iteration_order():
    # Enough explicit tags to distinguish canonical ordering from set iteration.
    tags = ["zulu", "bravo", "alpha", "kilo", "delta", "hotel", "echo", "golf", "foxtrot"]
    for order in (tags, list(reversed(tags))):
        row = hs._conversation_row(_conv(1, tags=[{"tag": t} for t in order]), [], hs._Options())
        assert "Tags: " + ", ".join(sorted(tags)) in row["content"]


def test_untrusted_article_listing_never_deletes(tmp_path):
    fake = FakeHelpScout(collections=[{"id": "c1"}], articles=[_article("a1"), _article("a2")])
    _docs_run(tmp_path, fake)
    fake.articles = [_article("a1")]
    real = fake._docs

    def bad_count(base, query):
        response = real(base, query)
        if "/collections/" in base:
            response._payload["articles"]["count"] = 2
        return response

    fake._docs = bad_count
    pipeline, _ = _docs_run(tmp_path, fake)
    assert set(_rows(pipeline, "help_scout_articles")) == {"article:a1", "article:a2"}
    fake._docs = real
    pipeline, _ = _docs_run(tmp_path, fake)
    assert set(_rows(pipeline, "help_scout_articles")) == {"article:a1"}


def test_rate_stop_during_recheck_keeps_tombstones_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "MAX_VERIFY", 3)
    fake = FakeHelpScout([_conv(i) for i in range(1, 6)])
    _run(tmp_path, fake, reconcile_every=1)
    fake.conversations = fake.conversations[-1:]
    real = fake._inbox

    def limited(base, query):
        if base.endswith("/conversations/2"):
            return FakeResponse(429, headers={"X-RateLimit-Retry-After": "900"})
        return real(base, query)

    fake._inbox = limited
    pipeline, _ = _run(tmp_path, fake)
    assert "conversation:1" not in _rows(pipeline)
    assert 1 not in _state(pipeline)["known_ids"]
    assert _state(pipeline)["reconcile_pending"]
    fake._inbox = real
    fake.calls.clear()
    pipeline, _ = _run(tmp_path, fake)
    assert set(_rows(pipeline)) == {"conversation:5"}
    assert not any(url.endswith("/conversations/1") for url, _ in fake.calls)


def test_article_empty_middle_page_keeps_existing_documents(tmp_path):
    fake = FakeHelpScout(collections=[{"id": "c1"}], articles=[_article("a1"), _article("a2")])
    _docs_run(tmp_path, fake)
    fake.articles = [_article("a1")]
    real = fake._docs

    def broken(base, query):
        if "/collections/" in base:
            return FakeResponse(
                payload={"articles": {"page": 1, "pages": 2, "count": 2, "items": []}}
            )
        return real(base, query)

    fake._docs = broken
    pipeline, _ = _docs_run(tmp_path, fake)
    assert set(_rows(pipeline, "help_scout_articles")) == {"article:a1", "article:a2"}


def test_retry_merge_uses_latest_observation_for_same_id(tmp_path):
    fake = FakeHelpScout([_conv(i, modified=f"2026-01-01T00:00:0{i}Z") for i in range(1, 5)])

    def bump_and_edit(base, query):
        if query.get("sortField") == "modifiedAt" and query.get("page") == "2":
            fake.on_page = None
            fake.conversations[0]["_modified"] = "2026-01-01T00:01:00Z"
            fake.conversations[0]["_threads"][0]["body"] = "Latest retry content"

    fake.on_page = bump_and_edit
    pipeline, _ = _run(tmp_path, fake)
    assert "Latest retry content" in _rows(pipeline)["conversation:1"]
