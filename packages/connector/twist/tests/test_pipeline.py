"""twist_source through a real dlt pipeline into a temp sqlite destination.

This is the path cognee takes: rows land in a dlt table with merge +
hard-delete, and whatever is no longer in that table is forgotten by cognee's
orphan cleanup. So "gone from the table" here means "forgotten" in cognee.
"""

import pytest

from cognee_community_connector_twist import twist_source

dlt = pytest.importorskip("dlt")

LAUNCH = 852087


def _source(fake, **kwargs):
    kwargs.setdefault("full_sync_every", 0)
    return twist_source(
        fake.workspace_id, access_token=fake.token, http_client=fake.http_client(), **kwargs
    )


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="twist_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'tw.db').as_posix()}"),
        dataset_name="twist_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _rows(pipeline, fake, table=None):
    table = table or f"twist_{fake.workspace_id}"
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content FROM {table}") as cursor,
    ):
        return {row[0]: {"title": row[1], "content": row[2]} for row in cursor.fetchall()}


def test_source_declares_document_marker_and_its_own_pipeline(fake):
    from cognee.tasks.ingestion.dlt_utils import (
        PIPELINE_SCOPE_ATTR,
        document_source_tag,
        pipeline_name_for_source,
    )

    a = twist_source(1, access_token="t")
    b = twist_source(2, access_token="t")
    assert document_source_tag(a) == "twist"
    assert getattr(a, PIPELINE_SCOPE_ATTR) == "twist_1"
    # Each workspace gets its own dlt pipeline, never the shared default one.
    assert pipeline_name_for_source(a, "ds") != "ingest_dlt_source"
    assert pipeline_name_for_source(a, "ds") != pipeline_name_for_source(b, "ds")


def test_arguments(monkeypatch):
    # Settings come only from the arguments; the factory never reads the environment.
    monkeypatch.setenv("TWIST_WORKSPACE_ID", "254427")
    monkeypatch.setenv("TWIST_ACCESS_TOKEN", "t")
    with pytest.raises(TypeError):
        twist_source()
    with pytest.raises(ValueError, match="workspace_id"):
        twist_source("", access_token="t")
    with pytest.raises(ValueError, match="access token"):
        twist_source(1, access_token="")
    with pytest.raises(ValueError, match="full_sync_every"):
        twist_source(1, access_token="t", full_sync_every=-1)
    with pytest.raises(ValueError, match="max_delete_ratio"):
        twist_source(1, access_token="t", max_delete_ratio=0)
    with pytest.raises(TypeError, match="list of ids"):
        twist_source(1, access_token="t", channel_ids="852087")
    assert twist_source("254427", access_token="t", channel_ids=["852087"]).name == "twist_254427"


def test_first_sync_loads_threads_and_channels(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    rows = _rows(pipeline, seeded)
    assert len(rows) == 5  # 3 threads + 2 channels
    checkout = next(r for r in rows.values() if r["title"] == "Checkout redesign")
    assert "Agreed, let's go with option B." in checkout["content"]


def test_sync_stats_report_counts_only(seeded, tmp_path, thread_named):
    pipeline = _pipeline(tmp_path)
    source = _source(seeded)
    pipeline.run(source)
    assert source.cognee_sync_stats == {
        "scanned": 3,
        "changed": 3,
        "unchanged": 0,
        "deleted": 0,
        "skipped": 0,
        "held_back": 0,
        "failed": 0,
        "failed_auth": 0,
        "failed_rate_limit": 0,
    }
    seeded.delete_thread(thread_named("Launch date")["id"])
    source = _source(seeded)
    pipeline.run(source)
    assert source.cognee_sync_stats["deleted"] == 1
    assert all(isinstance(v, int) for v in source.cognee_sync_stats.values())


def test_edit_replaces_the_row_on_resync(seeded, tmp_path, thread_named):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    launch = thread_named("Launch date")
    seeded.edit_thread_body(launch["id"], "Target launch is 15 March.")
    pipeline.run(_source(seeded))
    content = _rows(pipeline, seeded)[f"thread:{launch['id']}"]["content"]
    assert "15 March" in content
    assert "1 March." not in content


def test_deleted_thread_is_removed_from_the_table(seeded, tmp_path, thread_named):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    launch = thread_named("Launch date")
    seeded.delete_thread(launch["id"])
    pipeline.run(_source(seeded))
    rows = _rows(pipeline, seeded)
    assert f"thread:{launch['id']}" not in rows
    assert len(rows) == 4


def test_failed_run_changes_nothing(seeded, tmp_path, thread_named, monkeypatch):
    monkeypatch.setattr("cognee_community_connector_twist.twist.time.sleep", lambda s: None)
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    before = _rows(pipeline, seeded)
    launch = thread_named("Launch date")
    seeded.delete_thread(launch["id"])

    seeded.fail_with = 503
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the TwistError
        pipeline.run(_source(seeded))
    assert _rows(pipeline, seeded) == before

    # The state did not move, so the next good run still sees the delete.
    seeded.fail_with = None
    pipeline.run(_source(seeded))
    assert f"thread:{launch['id']}" not in _rows(pipeline, seeded)


def test_mass_delete_is_confirmed_by_the_next_run(fake, tmp_path):
    fake.add_channel(LAUNCH, "launch")
    for i in range(12):
        fake.add_thread(LAUNCH, f"Thread {i}")
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(fake))
    for thread_id in list(fake.threads)[:11]:
        fake.delete_thread(thread_id)
    source = _source(fake)
    pipeline.run(source)  # no error: a scheduled sync keeps running
    assert source.cognee_sync_stats["held_back"] == 11
    assert len(_rows(pipeline, fake)) == 13
    # The held-back set lives in dlt state, so it survives into the next run.
    pipeline.run(_source(fake))
    assert len(_rows(pipeline, fake)) == 2


def test_failed_runs_report_a_category(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr("cognee_community_connector_twist.twist.time.sleep", lambda s: None)
    source = twist_source(
        seeded.workspace_id, access_token="oauth2:wrong", http_client=seeded.http_client()
    )
    with pytest.raises(Exception, match="Invalid token"):
        _pipeline(tmp_path / "a").run(source)
    assert (source.cognee_sync_stats["failed"], source.cognee_sync_stats["failed_auth"]) == (1, 1)

    seeded.fail_with = 429
    source = _source(seeded)
    with pytest.raises(Exception, match="429"):
        _pipeline(tmp_path / "b").run(source)
    stats = source.cognee_sync_stats
    assert (stats["failed"], stats["failed_rate_limit"], stats["failed_auth"]) == (1, 1, 0)


def test_two_workspaces_with_their_own_resource_names_stay_apart(seeded, tmp_path, thread_named):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded, resource_name="twist_a", channel_ids=[LAUNCH]))
    pipeline.run(_source(seeded, resource_name="twist_b", channel_ids=[852086]))
    a_before = _rows(pipeline, seeded, "twist_a")
    logo = thread_named("Logo refresh")
    seeded.delete_thread(logo["id"])
    pipeline.run(_source(seeded, resource_name="twist_b", channel_ids=[852086]))
    assert f"thread:{logo['id']}" not in _rows(pipeline, seeded, "twist_b")
    assert _rows(pipeline, seeded, "twist_a") == a_before


def test_check_active_can_stop_a_sync(seeded, tmp_path):
    calls = []

    def check_active():
        calls.append(1)
        if len(calls) > 3:
            raise PermissionError("credentials revoked")

    with pytest.raises(Exception, match="credentials revoked"):
        _pipeline(tmp_path).run(_source(seeded, check_active=check_active))
    # Stopped at the fourth check, long before the three threads were all read.
    assert len(calls) == 4


def test_one_resource_name_cannot_hold_two_workspaces(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded, resource_name="shared"))
    other = twist_source(
        111, access_token=seeded.token, http_client=seeded.http_client(), resource_name="shared"
    )
    with pytest.raises(Exception, match="own resource_name"):
        pipeline.run(other)
