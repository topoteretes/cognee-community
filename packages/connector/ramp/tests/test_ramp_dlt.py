"""The connector in a real dlt pipeline: a failed run commits nothing, and state is per
resource_name. (The sync tests use a plain dict, which keeps whatever a failed run wrote.)
"""

import json

import dlt
import httpx
import pytest
from fake_ramp import FakeRamp, ago

from cognee_community_connector_ramp import RampClient, ramp_source


def pipeline_for(tmp_path):
    return dlt.pipeline(
        pipeline_name="ramp_dlt_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'ramp.db').as_posix()}"),
        dataset_name="ramp_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def source(fake, name="ramp_a", client_id="client"):
    client = RampClient(client_id, "secret", http=fake.client(), sleep=lambda _s: None)
    return ramp_source(resource_name=name, service=client)


def resource_state(pipeline, name):
    sources = pipeline.state.get("sources", {})
    for source_state in sources.values():
        state = source_state.get("resources", {}).get(name)
        if state is not None:
            return json.loads(json.dumps(state))
    return None


def staged_ids(pipeline, table):
    with pipeline.sql_client() as client, client.execute_query(f"SELECT id FROM {table}") as cur:
        return {row[0] for row in cur.fetchall()}


def test_failed_run_commits_neither_rows_nor_state(tmp_path):
    fake = FakeRamp()
    fake.add("t1", ago(3), memo="Hold", state="PENDING")
    fake.add("t2", ago(2), memo="Taxi")
    pipeline = pipeline_for(tmp_path)
    pipeline.run(source(fake))
    before = resource_state(pipeline, "ramp_a")
    assert staged_ids(pipeline, "ramp_a") == {"transaction:t1", "transaction:t2"}

    del fake.transactions["t1"]
    fake.queued = [httpx.Response(500, content=b"{}")] * 10
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error
        pipeline.run(source(fake))

    assert resource_state(pipeline, "ramp_a") == before
    assert staged_ids(pipeline, "ramp_a") == {"transaction:t1", "transaction:t2"}

    fake.queued = []
    pipeline.run(source(fake))
    assert staged_ids(pipeline, "ramp_a") == {"transaction:t2"}


def test_each_resource_name_keeps_its_own_state(tmp_path):
    first, second = FakeRamp(), FakeRamp()
    first.credentials, second.credentials = ("client-a", "secret"), ("client-b", "secret")
    first.add("a1", ago(3), memo="Business A")
    second.add("b1", ago(3), memo="Business B")
    pipeline = pipeline_for(tmp_path)

    pipeline.run(source(first, "ramp_a", client_id="client-a"))
    pipeline.run(source(second, "ramp_b", client_id="client-b"))

    assert set(resource_state(pipeline, "ramp_a")["rows"]) == {"transaction:a1"}
    assert set(resource_state(pipeline, "ramp_b")["rows"]) == {"transaction:b1"}
    assert staged_ids(pipeline, "ramp_a") == {"transaction:a1"}
    assert staged_ids(pipeline, "ramp_b") == {"transaction:b1"}
