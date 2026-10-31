"""Delivery tests: a real dlt pipeline, and a stop that loses nothing.

Runs the source through a real dlt merge into SQLite (the pattern the Telegram
connector's delivery tests use) to prove that an unwatched movie physically
disappears from the destination — what cognee's ``orphan_cleanup`` reads —
that a revoked ``check_active`` loses nothing, and that a long rate-limit wait
ends the run cleanly with ``failed_rate_limit`` and replays on the next sync.
"""

import dlt
import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from fake_trakt import INCEPTION, SEVERANCE, TENET, FakeTrakt

from cognee_community_connector_trakt import trakt_source


@pytest.fixture(autouse=True)
def no_active_pipeline():
    """Iterating a resource outside a run uses the active pipeline's state, so
    make sure a pipeline left active by an earlier test cannot leak it in."""
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()


def _pipeline(tmp_path, name="trakt_delivery"):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'trakt.db'}"),
        dataset_name="trakt",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table="trakt_items"):
    with pipeline.sql_client() as client:
        try:
            return [r[0] for r in client.execute_sql(f"SELECT id FROM {table} ORDER BY id")]
        except DatabaseUndefinedRelation:  # nothing loaded into this table yet
            return []


def _contents(pipeline, table="trakt_items"):
    with pipeline.sql_client() as client:
        return dict(client.execute_sql(f"SELECT id, content FROM {table}"))


def test_merge_upserts_rewatches_and_physically_deletes_unwatched_movies(tmp_path):
    pipeline = _pipeline(tmp_path)
    fake = FakeTrakt()

    pipeline.run(trakt_source(client=fake))
    ids = _ids(pipeline)
    assert "movie:1" in ids and "movie:3" in ids and "episode:204" in ids
    assert "Watched: 2 plays" in _contents(pipeline)["movie:1"]

    fake.watched_movies[0]["plays"] = 3  # rewatched Inception
    fake.unwatch_movie(TENET["ids"]["trakt"])  # Tenet removed from history
    pipeline.run(trakt_source(client=fake))

    ids = _ids(pipeline)
    assert "movie:3" not in ids, "an unwatched movie is physically deleted"
    assert "movie:1" in ids and "Watched: 3 plays" in _contents(pipeline)["movie:1"]
    assert "episode:204" in ids and "show:10" in ids, "untouched sections stay"


def test_unwatched_episodes_are_physically_deleted(tmp_path):
    pipeline = _pipeline(tmp_path, name="trakt_episodes")
    fake = FakeTrakt()
    pipeline.run(trakt_source(client=fake))
    assert "episode:204" in _ids(pipeline)

    fake.unwatch_show_season(SEVERANCE["ids"]["trakt"], 2)
    pipeline.run(trakt_source(client=fake))
    ids = _ids(pipeline)
    assert "episode:204" not in ids
    assert "episode:201" in ids, "still rated, so it keeps its rating document"
    assert "episode:101" in ids and "show:10" in ids


def test_a_long_rate_limit_ends_the_run_cleanly_and_the_next_sync_replays(tmp_path):
    pipeline = _pipeline(tmp_path, name="trakt_rate_limit")
    fake = FakeTrakt()
    fake.fail_once = ("rate_limit", 3600)
    source = trakt_source(client=fake)
    pipeline.run(source)  # does not fail the host's run
    assert _ids(pipeline) == []
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert source.cognee_sync_stats["items"] == 0
    assert all(isinstance(value, int) for value in source.cognee_sync_stats.values())

    pipeline.run(trakt_source(client=fake))  # the next sync resumes
    assert len(_ids(pipeline)) == 10


def test_revoked_access_mid_run_loses_nothing(tmp_path):
    pipeline = _pipeline(tmp_path, name="trakt_revoked")
    fake = FakeTrakt()
    checks = {"n": 0}

    def revoke_between_rows():
        checks["n"] += 1
        if checks["n"] > 5:  # past the API calls, between rows
            raise PermissionError("access revoked")

    with pytest.raises(Exception, match="access revoked"):
        pipeline.run(trakt_source(client=fake, check_active=revoke_between_rows))
    assert _ids(pipeline) == []

    for _ in range(2):
        pipeline.run(trakt_source(client=fake))
    assert len(_ids(pipeline)) == 10, "every row arrives in later runs"
    assert "Watched: 2 plays" in _contents(pipeline)["movie:1"]
    assert INCEPTION["title"] in _contents(pipeline)["movie:1"]


def test_a_failed_run_replays_the_same_first_sync(tmp_path):
    pipeline = _pipeline(tmp_path, name="trakt_replay")
    fake = FakeTrakt()

    class ExplodingRows(FakeTrakt):
        def request(self, path, params=None):
            if path == "/users/me/watched/movies":
                raise RuntimeError("bad payload")
            return super().request(path, params)

    with pytest.raises(Exception, match="bad payload"):
        pipeline.run(trakt_source(client=ExplodingRows()))
    assert _ids(pipeline) == []

    pipeline.run(trakt_source(client=fake))  # nothing was confirmed: full pull again
    assert len(_ids(pipeline)) == 10
