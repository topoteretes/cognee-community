"""Paging, bucketing, the timestamp cursor and expiry, driven through a fake Loki."""

import pytest
from fake_loki import MINUTE, T0, FakeLoki

from cognee_community_connector_loki.loki import _iter_rows, fetch_window, loki_source

CHECKOUT = {"service_name": "checkout", "level": "error"}
PAYMENTS = {"service_name": "payments", "level": "error"}
SELECTOR = '{level="error"}'


def run(fake, state, now, *, selectors=(SELECTOR,), **overrides):
    settings = {
        "bucket": 5 * MINUTE,
        "lookback": 60 * MINUTE,
        "keep_for": 24 * 60 * MINUTE,
        "query_window": 60 * MINUTE,
        "overlap_buckets": 1,
        "page_limit": 5000,
        "max_lines": 100_000,
        "max_lines_per_bucket": 1000,
    }
    settings.update(overrides)
    stats = {}
    rows = list(_iter_rows(fake, state, stats, selectors=list(selectors), now=now, **settings))
    return rows, stats


def live(rows):
    return [row for row in rows if not row["_deleted"]]


# ---------------------------------------------------------------------------
# paging
# ---------------------------------------------------------------------------
def test_paging_reads_every_line_of_a_window_once():
    fake = FakeLoki()
    for i in range(23):
        labels = CHECKOUT if i % 2 else PAYMENTS
        fake.push(labels, T0 + i * 1_000_000, f"line {i}")

    entries = fetch_window(fake, SELECTOR, T0, T0 + MINUTE, page_limit=5)

    assert sorted(line for _, _, _, line, _ in entries) == sorted(f"line {i}" for i in range(23))
    assert len(fake.calls) > 4


def test_lines_that_share_a_timestamp_across_a_split_are_all_read():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0, "a")
    fake.push(CHECKOUT, T0 + 1, "b")
    fake.push(CHECKOUT, T0 + 2, "c1")
    fake.push(PAYMENTS, T0 + 2, "c2")
    fake.push(PAYMENTS, T0 + 2, "c3")
    fake.push(PAYMENTS, T0 + 3, "d")

    entries = fetch_window(fake, SELECTOR, T0, T0 + MINUTE, page_limit=3)

    assert sorted(line for _, _, _, line, _ in entries) == ["a", "b", "c1", "c2", "c3", "d"]


def test_a_page_cut_per_stream_still_reads_every_line():
    # loki can cut a full answer at different times per stream, not at the oldest
    # lines overall, so the newest timestamp of a page is not a safe place to resume
    fake = FakeLoki(cut_per_stream=True)
    for i in range(300):
        fake.push(CHECKOUT, T0 + i * 1000, f"checkout {i}")
        fake.push(PAYMENTS, T0 + i * 1000 + 7, f"payments {i}")

    entries = fetch_window(fake, SELECTOR, T0, T0 + MINUTE, page_limit=50)

    lines = [line for _, _, _, line, _ in entries]
    assert len(lines) == 600
    assert len(set(lines)) == 600


def test_more_lines_in_one_nanosecond_than_a_page_moves_on_instead_of_looping():
    fake = FakeLoki()
    for i in range(4):
        fake.push(CHECKOUT, T0, f"same {i}")
    fake.push(CHECKOUT, T0 + 5, "later")

    entries = fetch_window(fake, SELECTOR, T0, T0 + MINUTE, page_limit=2)

    lines = [line for _, _, _, line, _ in entries]
    assert "later" in lines
    assert len([line for line in lines if line.startswith("same")]) == 2
    assert len(fake.calls) < 200


# ---------------------------------------------------------------------------
# bucketing
# ---------------------------------------------------------------------------
def test_lines_are_grouped_per_stream_and_bucket_with_labels():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "timeout calling payments")
    fake.push(CHECKOUT, T0 + 2 * MINUTE, '{"msg": "retry failed", "attempt": 3}')
    fake.push(CHECKOUT, T0 + 6 * MINUTE, "circuit open")
    fake.push(PAYMENTS, T0 + 1 * MINUTE, "db pool exhausted")

    rows, stats = run(fake, {}, T0 + 10 * MINUTE)

    assert stats["emitted"] == 3
    assert len({row["id"] for row in rows}) == 3
    first = next(row for row in rows if "timeout" in row["content"])
    assert first["title"] == "checkout logs 2026-01-01 00:00 UTC"
    assert 'Labels: level="error", service_name="checkout"' in first["content"]
    assert "Lines: 2" in first["content"]
    assert "2026-01-01T00:01:00.000Z timeout calling payments" in first["content"]
    # structured lines are kept as they are
    assert '{"msg": "retry failed", "attempt": 3}' in first["content"]
    assert "circuit open" not in first["content"]


def test_structured_metadata_is_appended_to_its_line():
    answer = [
        {
            "stream": CHECKOUT,
            "values": [
                [
                    str(T0 + MINUTE),
                    "payment failed",
                    {
                        "structuredMetadata": {"trace_id": "abc123", "detected_level": "error"},
                        "parsed": {"code": "502"},
                    },
                ]
            ],
        }
    ]

    class Answering:
        def query_range(self, query, start, end, limit):
            return answer if start <= T0 + MINUTE < end else []

    rows, _ = run(Answering(), {}, T0 + 5 * MINUTE)

    assert 'payment failed code="502", trace_id="abc123"' in rows[0]["content"]


def test_only_whole_buckets_are_read():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "done")
    fake.push(CHECKOUT, T0 + 7 * MINUTE, "bucket still open")

    rows, _ = run(fake, {}, T0 + 8 * MINUTE)

    assert [row["content"].count("done") for row in rows] == [1]
    assert all("still open" not in row["content"] for row in rows)


def test_a_noisy_bucket_is_cut_and_says_so():
    fake = FakeLoki()
    for i in range(5):
        fake.push(CHECKOUT, T0 + i, f"line {i}")

    rows, _ = run(fake, {}, T0 + 5 * MINUTE, max_lines_per_bucket=2)

    assert "line 1" in rows[0]["content"]
    assert "line 2" not in rows[0]["content"]
    assert "Lines: 5" in rows[0]["content"]
    assert "3 more lines in this window are not shown" in rows[0]["content"]


# ---------------------------------------------------------------------------
# incremental sync
# ---------------------------------------------------------------------------
def test_a_second_run_reads_only_new_buckets_without_duplicates():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "first")
    state = {}
    first, _ = run(fake, state, T0 + 10 * MINUTE)

    fake.push(CHECKOUT, T0 + 12 * MINUTE, "second")
    second, stats = run(fake, state, T0 + 15 * MINUTE)

    assert [row["content"].count("first") for row in first] == [1]
    assert [("second" in row["content"]) for row in second] == [True]
    assert stats["skipped"] == 0
    # the overlap bucket is read again but nothing in it changed
    assert fake.calls[-1][1] == T0 + 5 * MINUTE


def test_unchanged_buckets_are_not_emitted_again():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 6 * MINUTE, "only line")
    state = {}
    run(fake, state, T0 + 10 * MINUTE)

    rows, stats = run(fake, state, T0 + 10 * MINUTE)

    assert rows == []
    assert stats["skipped"] == 1


def test_a_late_line_in_the_overlap_updates_its_bucket():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 6 * MINUTE, "on time")
    state = {}
    (before,), _ = run(fake, state, T0 + 10 * MINUTE)

    fake.push(CHECKOUT, T0 + 7 * MINUTE, "arrived late")
    rows, _ = run(fake, state, T0 + 15 * MINUTE)

    (after,) = live(rows)
    assert after["id"] == before["id"]
    assert "on time" in after["content"]
    assert "arrived late" in after["content"]


def test_a_long_gap_is_read_in_windows_under_the_query_limit():
    fake = FakeLoki()
    state = {}
    run(fake, state, T0 + 10 * MINUTE)
    fake.calls.clear()

    run(fake, state, T0 + 10 * MINUTE + 3 * 60 * MINUTE, query_window=60 * MINUTE)

    assert len(fake.calls) == 4
    assert all(end - start <= 60 * MINUTE for _, start, end, _ in fake.calls)


def test_the_line_budget_stops_a_run_and_the_next_one_resumes():
    fake = FakeLoki()
    for i in range(6):
        fake.push(CHECKOUT, T0 + i * 60 * MINUTE + MINUTE, f"hour {i}")
    state = {}
    now = T0 + 6 * 60 * MINUTE

    first, stats = run(fake, state, now, lookback=6 * 60 * MINUTE, max_lines=2)
    second, _ = run(fake, state, now, lookback=6 * 60 * MINUTE, max_lines=100)

    assert stats["cut_short"] == 1
    assert len(first) == 2
    assert sorted(row["content"].split()[-1] for row in first + second) == [
        "0",
        "1",
        "2",
        "3",
        "4",
        "5",
    ]


def test_each_selector_keeps_its_own_cursor():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "checkout down")
    fake.push(PAYMENTS, T0 + 1 * MINUTE, "payments down")
    selectors = ('{service_name="checkout"}', '{service_name="payments"}')

    rows, _ = run(fake, {}, T0 + 5 * MINUTE, selectors=selectors)

    assert sorted(row["title"].split()[0] for row in rows) == ["checkout", "payments"]


# ---------------------------------------------------------------------------
# forgetting
# ---------------------------------------------------------------------------
def test_buckets_past_keep_for_are_forgotten():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "old")
    state = {}
    (old,), _ = run(fake, state, T0 + 5 * MINUTE)

    rows, stats = run(fake, state, T0 + 5 * MINUTE + 61 * MINUTE, keep_for=60 * MINUTE)

    assert rows == [{"id": old["id"], "_deleted": True}]
    assert stats["deleted"] == 1
    assert state["buckets"] == {}


def test_keep_for_none_keeps_everything():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "old")
    state = {}
    run(fake, state, T0 + 5 * MINUTE)

    rows, _ = run(fake, state, T0 + 400 * 24 * 60 * MINUTE, keep_for=None)

    assert [row for row in rows if row["_deleted"]] == []


def test_lines_already_past_keep_for_are_not_ingested():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "ancient")

    rows, _ = run(fake, {}, T0 + 120 * MINUTE, lookback=180 * MINUTE, keep_for=60 * MINUTE)

    assert rows == []


def test_removing_a_selector_forgets_its_documents():
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "checkout down")
    fake.push(PAYMENTS, T0 + 1 * MINUTE, "payments down")
    state = {}
    checkout, payments = '{service_name="checkout"}', '{service_name="payments"}'
    first, _ = run(fake, state, T0 + 5 * MINUTE, selectors=(checkout, payments))

    rows, _ = run(fake, state, T0 + 5 * MINUTE, selectors=(checkout,))

    gone = next(row["id"] for row in first if "payments" in row["title"])
    assert rows == [{"id": gone, "_deleted": True}]
    assert len(state["cursors"]) == 1


# ---------------------------------------------------------------------------
# the dlt resource
# ---------------------------------------------------------------------------
def source(fake, **kwargs):
    return loki_source(selectors=[SELECTOR], resource_name="loki_test", service=fake, **kwargs)


def test_the_resource_is_configured_for_merge_and_hard_delete():
    resource = source(FakeLoki())

    assert resource.name == "loki_test"
    schema = resource.compute_table_schema()
    disposition = schema.get("write_disposition")
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True
    assert resource.cognee_document_source == "loki"
    assert resource.cognee_pipeline_scope == "loki_test"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"selectors": []}, "selector"),
        ({"selectors": [SELECTOR, SELECTOR]}, "same selector"),
        ({"resource_name": " "}, "resource_name"),
        ({"service": None}, "base_url"),
        ({"bucket_minutes": 0}, "positive"),
        ({"keep_for_days": 0}, "keep_for_days"),
        ({"page_limit": 0}, "positive"),
    ],
)
def test_bad_settings_fail_at_construction(kwargs, message):
    settings = {"selectors": [SELECTOR], "resource_name": "loki_test", "service": FakeLoki()}
    settings.update(kwargs)
    with pytest.raises(ValueError, match=message):
        loki_source(**settings)


def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="loki_e2e_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'loki.db'}"),
        dataset_name="loki_e2e",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def test_check_active_can_stop_a_run(tmp_path):
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "line")

    def revoked():
        raise PermissionError("access revoked")

    resource = source(fake, check_active=revoked, clock=lambda: T0 + 5 * MINUTE)
    with pytest.raises(Exception, match="access revoked"):
        _pipeline(tmp_path).run(resource)
    assert fake.calls == []


def test_dlt_merge_removes_an_expired_bucket(tmp_path):
    """Offline end to end: a real dlt merge drops the row an expired bucket marks deleted."""
    fake = FakeLoki()
    fake.push(CHECKOUT, T0 + 1 * MINUTE, "old")
    fake.push(CHECKOUT, T0 + 100 * MINUTE, "new")

    def sync(now):
        pipeline = _pipeline(tmp_path)
        pipeline.run(source(fake, clock=lambda: now, lookback_hours=3, keep_for_days=1 / 24))
        with pipeline.sql_client() as client:
            rows = client.execute_sql("SELECT content FROM loki_test")
        return sorted(row[0].splitlines()[-1].split()[-1] for row in rows)

    assert sync(T0 + 55 * MINUTE) == ["old"]
    assert sync(T0 + 105 * MINUTE) == ["new"]
