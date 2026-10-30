"""The connector in a real dlt pipeline: a failed run commits nothing, and two Salesforce
orgs can share one dataset without clobbering each other.

(The sync tests drive a plain dict, which keeps whatever a failed run wrote.)
"""

import json

import dlt
import httpx
import pytest
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_salesforce import FakeSalesforce, client_for

from cognee_community_connector_salesforce import salesforce_source
from cognee_community_connector_salesforce.salesforce import SalesforceUnavailableError


def pipeline_for(tmp_path):
    return dlt.pipeline(
        pipeline_name="salesforce_dlt_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'sf.db').as_posix()}"),
        dataset_name="salesforce_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def source(fake, **kwargs):
    return salesforce_source(service=client_for(fake), **kwargs)


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
    fake = FakeSalesforce()
    alpha = fake.add("Account", Name="Alphacorp")
    bravo = fake.add("Account", Name="Bravocorp")
    pipeline = pipeline_for(tmp_path)
    pipeline.run(source(fake))
    table = "salesforce_acme_dev_ed"
    before = resource_state(pipeline, table)
    assert staged_ids(pipeline, table) == {f"account:{alpha}", f"account:{bravo}"}

    fake.delete("Account", alpha)
    fake.tick(60)  # getDeleted needs a window of a minute or more
    fake.queued = [httpx.Response(500, content=b"{}")] * 10
    with pytest.raises(PipelineStepFailed) as failure:
        pipeline.run(source(fake))
    # the cause, so this cannot pass on an unrelated failure
    causes = []
    error: BaseException | None = failure.value
    while error is not None:
        causes.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(c, SalesforceUnavailableError) for c in causes)

    assert resource_state(pipeline, table) == before
    assert staged_ids(pipeline, table) == {f"account:{alpha}", f"account:{bravo}"}

    fake.queued = []
    pipeline.run(source(fake))
    assert staged_ids(pipeline, table) == {f"account:{bravo}"}, "the hard delete removes the row"


def test_two_orgs_in_one_dataset_keep_their_own_tables_and_state(tmp_path):
    first = FakeSalesforce(login="https://acme-dev-ed.my.salesforce.com")
    second = FakeSalesforce(org_id="00Dg500000GLOBEXAA", login="https://globex.my.salesforce.com")
    a = first.add("Account", Name="Alphacorp")
    b = second.add("Account", Name="Bravocorp")
    pipeline = pipeline_for(tmp_path)

    pipeline.run(source(first))
    pipeline.run(source(second))

    assert set(resource_state(pipeline, "salesforce_acme_dev_ed")["rows"]) == {f"account:{a}"}
    assert set(resource_state(pipeline, "salesforce_globex")["rows"]) == {f"account:{b}"}
    assert staged_ids(pipeline, "salesforce_acme_dev_ed") == {f"account:{a}"}
    assert staged_ids(pipeline, "salesforce_globex") == {f"account:{b}"}


def test_the_resource_closes_the_client_it_made(monkeypatch, tmp_path):
    """A scheduled sync builds a source per load; an unclosed pool leaks every time."""
    from cognee_community_connector_salesforce import salesforce as module

    fake = FakeSalesforce()
    fake.add("Account", Name="Alphacorp")
    closed: list[str] = []
    real_client = module.SalesforceClient

    def build(login, client_id, client_secret, *, api_version, check_active=None):
        client = real_client(login, fake.client_id, fake.client_secret, http=fake.client())
        monkeypatch.setattr(client, "close", lambda: closed.append("closed"))
        return client

    monkeypatch.setattr(module, "SalesforceClient", build)
    pipeline_for(tmp_path).run(
        salesforce_source(login_url=fake.login, client_id="a", client_secret="b")
    )

    assert closed == ["closed"]
