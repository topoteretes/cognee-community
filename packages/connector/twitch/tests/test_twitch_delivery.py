"""No row is ever lost, and deletions reach the destination.

These drive a real dlt pipeline (merge + hard delete into SQLite, the
telegram delivery-test pattern) against ``FakeTwitch``. Coverage:

  - a full first sync lands every row in the table
  - an unfollow physically deletes the channel's row and all of its content,
    an expired VOD physically disappears, a new VOD is ingested, and a
    renamed channel upserts its row under the same key
  - a long rate-limit wait ends the run cleanly (``failed_rate_limit``),
    loads nothing, and the next run replays in full
  - a host revoking access mid-run fails the load; every row still arrives
    in the next run
  - a sync state written by another Twitch user is refused
"""

import dlt
import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from fake_twitch import OTHER_USER, VIDEO_RUST_NEW, FakeTwitch

from cognee_community_connector_twitch import twitch_source

TOKEN = "secret-access-token-value"


@pytest.fixture(autouse=True)
def no_active_pipeline():
    """Iterating a resource outside a run uses the active pipeline's state, so
    make sure a pipeline left active by an earlier test cannot leak its
    cursor in."""
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()


def _pipeline(tmp_path, name="twitch_delivery"):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'twitch.db'}"),
        dataset_name="twitch",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table="twitch_testuser"):
    with pipeline.sql_client() as client:
        try:
            return [row[0] for row in client.execute_sql(f"SELECT id FROM {table} ORDER BY id")]
        except DatabaseUndefinedRelation:  # nothing loaded into this table yet
            return []


def _rows(pipeline, table="twitch_testuser"):
    with pipeline.sql_client() as client:
        return {
            row[0]: row[1]
            for row in client.execute_sql(f"SELECT id, content FROM {table} ORDER BY id")
        }


def _source(fake, **kwargs):
    return twitch_source(client_id="id", access_token=TOKEN, client=fake, **kwargs)


FIRST_RUN_IDS = [
    "channel:2001",
    "channel:2002",
    "channel:2003",
    "clip:RandomClip1",
    "clip:RandomClip2",
    "video:1234567",
    "video:1234568",
    "video:1234569",
]


def test_a_full_first_sync_lands_every_row(tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(FakeTwitch()))
    assert _ids(pipeline) == sorted(FIRST_RUN_IDS)


def test_unfollow_expired_new_and_renamed_rows_reach_the_table(tmp_path):
    pipeline = _pipeline(tmp_path)
    fake = FakeTwitch()
    pipeline.run(_source(fake))
    assert _ids(pipeline) == sorted(FIRST_RUN_IDS)

    fake.unfollow("2003")  # a bare channel: only its own row goes
    fake.expire_video("1234569")  # Twitch deleted this VOD
    fake.add_video("2001", VIDEO_RUST_NEW)
    fake.rename_channel("2002", "codingcorner", "CodingCorner")
    pipeline.run(_source(fake))

    # deleted rows are physically gone: this is what cognee's orphan cleanup reads
    assert _ids(pipeline) == sorted(
        [
            "channel:2001",
            "channel:2002",
            "clip:RandomClip1",
            "clip:RandomClip2",
            "video:1234567",
            "video:1234568",
            "video:1234570",
        ]
    )
    rows = _rows(pipeline)
    # the new VOD arrived with its content
    assert "Building a compiler, part 4" in rows["video:1234570"]
    # the renamed channel upserted its row under the same key
    assert "CodingCorner (codingcorner)" in rows["channel:2002"]


def test_a_long_rate_limit_wait_ends_the_run_cleanly_and_replays(tmp_path):
    pipeline = _pipeline(tmp_path)
    fake = FakeTwitch()
    fake.long_wait = ("videos", "2001")
    source = _source(fake)
    pipeline.run(source)
    assert _ids(pipeline) == [], "a clean stop loads nothing"
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert all(type(value) is int for value in source.cognee_sync_stats.values())

    fake.long_wait = None
    pipeline.run(_source(fake))
    assert _ids(pipeline) == sorted(FIRST_RUN_IDS)


def test_check_active_revoked_mid_run_loses_nothing(tmp_path):
    pipeline = _pipeline(tmp_path)
    fake = FakeTwitch()
    checks = {"n": 0}

    def revoke_after_a_few_checks():
        checks["n"] += 1
        if checks["n"] > 3:  # past validation, during the listing
            raise PermissionError("access revoked")

    with pytest.raises(Exception, match="access revoked"):
        pipeline.run(_source(fake, check_active=revoke_after_a_few_checks))
    assert _ids(pipeline) == [], "a failed load commits nothing"

    pipeline.run(_source(fake))
    assert _ids(pipeline) == sorted(FIRST_RUN_IDS), "every row still arrives"


def test_a_sync_state_from_another_user_is_refused(tmp_path):
    pipeline = _pipeline(tmp_path)
    first = FakeTwitch()
    pipeline.run(_source(first, resource_name="shared"))
    assert _ids(pipeline, "shared") == sorted(FIRST_RUN_IDS)

    other = FakeTwitch(user=OTHER_USER)
    with pytest.raises(Exception, match="another Twitch user"):
        pipeline.run(_source(other, resource_name="shared"))
    follow_calls = other.calls_to("channels/followed")
    assert follow_calls == [], "the second user never read anything"
    assert _ids(pipeline, "shared") == sorted(FIRST_RUN_IDS), "the first user's rows stay"
