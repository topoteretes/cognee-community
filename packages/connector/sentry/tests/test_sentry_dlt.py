"""The connector in a real dlt pipeline: a failed run commits nothing, and two Sentry
organizations can share one dataset without clobbering each other.

(The sync tests drive a plain dict, which keeps whatever a failed run wrote.)
"""

import json

import dlt
import httpx
import pytest
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_sentry import FakeSentry, ago

from cognee_community_connector_sentry import SentryClient, sentry_source


def pipeline_for(tmp_path):
    return dlt.pipeline(
        pipeline_name="sentry_dlt_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'sentry.db').as_posix()}"),
        dataset_name="sentry_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def source(fake, **kwargs):
    client = SentryClient(
        fake.valid_token, org_slug=fake.org, http=fake.client(), sleep=lambda _s: None
    )
    return sentry_source(service=client, org_slug=fake.org, **kwargs)


def resource_state(pipeline, name):
    for source_state in pipeline.state.get("sources", {}).values():
        state = source_state.get("resources", {}).get(name)
        if state is not None:
            return json.loads(json.dumps(state))
    return None


def staged_ids(pipeline, table):
    with pipeline.sql_client() as client, client.execute_query(f"SELECT id FROM {table}") as cur:
        return {row[0] for row in cur.fetchall()}


def test_failed_run_commits_neither_rows_nor_state(tmp_path):
    fake = FakeSentry()
    fake.add_issue("i1", last_seen=ago(2))
    fake.add_issue("i2", last_seen=ago(1))
    pipeline = pipeline_for(tmp_path)
    pipeline.run(source(fake))
    table = "sentry_mergetober_test"
    before = resource_state(pipeline, table)
    assert staged_ids(pipeline, table) == {"issue:i1", "issue:i2"}

    fake.delete_issue("i1")
    fake.queued = [httpx.Response(500, content=b"{}")] * 10
    with pytest.raises(PipelineStepFailed) as failure:
        pipeline.run(source(fake))
    # name the wrapper and its cause, so this cannot pass on an unrelated failure
    assert isinstance(failure.value.__cause__, Exception)
    assert "sentry" in str(failure.value).lower()

    assert resource_state(pipeline, table) == before
    assert staged_ids(pipeline, table) == {"issue:i1", "issue:i2"}

    fake.queued = []
    pipeline.run(source(fake))
    assert staged_ids(pipeline, table) == {"issue:i2"}, "the hard delete removes the row"


def test_two_organizations_in_one_dataset_keep_their_own_tables_and_state(tmp_path):
    first, second = FakeSentry(org="acme"), FakeSentry(org="globex")
    first.add_issue("a1")
    second.add_issue("b1")
    pipeline = pipeline_for(tmp_path)

    pipeline.run(source(first))
    pipeline.run(source(second))

    assert set(resource_state(pipeline, "sentry_acme")["rows"]) == {"issue:a1"}
    assert set(resource_state(pipeline, "sentry_globex")["rows"]) == {"issue:b1"}
    assert staged_ids(pipeline, "sentry_acme") == {"issue:a1"}
    assert staged_ids(pipeline, "sentry_globex") == {"issue:b1"}
