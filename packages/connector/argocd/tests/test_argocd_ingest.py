"""A real ``dlt.pipeline`` run into SQLite, the way cognee stages a document source.

The table that cognee later reads back holds one row per application and per sync. No
network and no LLM: the Argo CD side is the in-memory fake.
"""

import json

import dlt
import pytest
from fake_argocd import FakeArgoSession, make_app, make_history

from cognee_community_connector_argocd import argocd_source

TOKEN = "s3cret-token-value"
GUESTBOOK = "argocd:prod:argocd/guestbook"
WEB = "argocd:prod:argocd/web"


def apps():
    return [
        make_app("guestbook", history=[make_history(1), make_history(2)]),
        make_app("web", health="Degraded", sync="OutOfSync"),
    ]


def run(tmp_path, session, **options):
    """One sync into a persistent pipeline. Returns (pipeline, source)."""
    pipeline = dlt.pipeline(
        pipeline_name="test_argocd_ingest",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'argocd.db'}"),
        dataset_name="argocd",
    )
    source = argocd_source(
        "https://argocd.example.com",
        api_token=TOKEN,
        instance_name="prod",
        session=session,
        **options,
    )
    pipeline.run(source, write_disposition="merge", primary_key="id")
    return pipeline, source


def rows(pipeline) -> dict[str, str]:
    with pipeline.sql_client() as client:
        result = client.execute_sql("SELECT id, content FROM argocd_applications ORDER BY id")
    return {row[0]: row[1] for row in result}


def find(node, key):
    """First value stored under ``key`` anywhere in the pipeline state."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = find(value, key)
            if found is not None:
                return found
    return None


def test_initial_sync_stages_every_application_and_history_entry(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    staged = rows(pipeline)
    assert set(staged) == {GUESTBOOK, f"{GUESTBOOK}:history:1", f"{GUESTBOOK}:history:2", WEB}
    assert "Health: Degraded" in staged[WEB]
    assert "Initiated by: user admin" in staged[f"{GUESTBOOK}:history:1"]


def test_an_unchanged_rerun_emits_nothing_and_keeps_every_row(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    first = rows(pipeline)
    pipeline, source = run(tmp_path, FakeArgoSession(apps()))
    assert rows(pipeline) == first
    assert source.cognee_sync_stats["unchanged"] == 2
    assert source.cognee_sync_stats["applications"] == 0


def test_a_zero_row_run_still_saves_the_new_cursor(tmp_path):
    run(tmp_path, FakeArgoSession(apps()))
    newer = apps()
    newer[1] = make_app("web", health="Degraded", sync="OutOfSync", resource_version="777")
    newer[0] = make_app(
        "guestbook", history=[make_history(1), make_history(2)], resource_version="778"
    )
    pipeline, source = run(tmp_path, FakeArgoSession(newer))
    assert source.cognee_sync_stats["applications"] == 0
    stored = find(pipeline.state, "instances")["prod"]["apps"]
    assert stored[WEB]["rv"] == "777"
    assert stored[GUESTBOOK]["rv"] == "778"


def test_a_changed_application_is_updated_in_place(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    first = rows(pipeline)
    changed = apps()
    changed[1] = make_app("web", health="Healthy", sync="Synced", resource_version="101")
    pipeline, _ = run(tmp_path, FakeArgoSession(changed))
    second = rows(pipeline)
    assert second[WEB] != first[WEB]
    assert "Health: Healthy" in second[WEB]
    assert len(second) == 4


def test_a_new_history_entry_is_added_without_touching_the_rest(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    first = rows(pipeline)
    more = apps()
    more[0] = make_app(
        "guestbook",
        history=[make_history(1), make_history(2), make_history(3)],
        resource_version="101",
    )
    pipeline, _ = run(tmp_path, FakeArgoSession(more))
    second = rows(pipeline)
    assert set(second) - set(first) == {f"{GUESTBOOK}:history:3"}
    assert {k: v for k, v in second.items() if k in first} == first


def test_forgotten_applications_are_physically_gone_after_the_resync(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    assert len(rows(pipeline)) == 4
    pipeline, source = run(tmp_path, FakeArgoSession([apps()[1]]))
    assert set(rows(pipeline)) == {WEB}  # the application and both history rows are gone
    assert source.cognee_sync_stats["deleted"] == 1


def test_an_application_being_deleted_is_removed_from_the_table(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    dying = make_app("web", resource_version="101")
    dying["metadata"]["deletionTimestamp"] = "2026-10-17T11:00:00Z"
    pipeline, _ = run(tmp_path, FakeArgoSession([apps()[0], dying]))
    assert WEB not in rows(pipeline)


def test_an_api_failure_fails_the_run_and_leaves_the_table_unchanged(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    before = rows(pipeline)
    session = FakeArgoSession([])  # an empty list must not read as "all deleted"
    session.script = [RuntimeError("Argo CD is down")]
    with pytest.raises(Exception, match="Argo CD is down"):
        run(tmp_path, session)
    assert rows(pipeline) == before
    # The next healthy run carries on from the saved state: nothing to do.
    pipeline, source = run(tmp_path, FakeArgoSession(apps()))
    assert rows(pipeline) == before
    assert source.cognee_sync_stats["deleted"] == 0


def test_an_empty_list_fails_the_run_instead_of_forgetting_everything(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    before = rows(pipeline)
    with pytest.raises(Exception, match="no applications"):
        run(tmp_path, FakeArgoSession([]))
    assert rows(pipeline) == before
    pipeline, _ = run(tmp_path, FakeArgoSession([]), allow_mass_delete=True)
    assert rows(pipeline) == {}


def test_the_saved_state_has_no_secrets(tmp_path):
    pipeline, _ = run(tmp_path, FakeArgoSession(apps()))
    dumped = json.dumps(pipeline.state, default=str)
    assert TOKEN not in dumped
    assert "argocd.example.com" not in dumped
    assert GUESTBOOK in dumped
