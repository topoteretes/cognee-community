"""mailchimp_source through a real dlt pipeline into a temp sqlite destination.

This is the path cognee takes: rows land in a dlt table with merge +
hard-delete, and whatever is no longer in that table is forgotten by cognee's
orphan cleanup. So "gone from the table" here means "forgotten" in cognee.
"""

import httpx
import pytest

from cognee_community_connector_mailchimp import mailchimp_source

dlt = pytest.importorskip("dlt")

KEY = "0123456789abcdef" * 2 + "-us22"  # built, so no key-shaped literal
TABLE = "mailchimp_us22"


def _source(fake, **kwargs):
    return mailchimp_source(fake.api_key, http_client=fake.http_client(), **kwargs)


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="mailchimp_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'mc.db').as_posix()}"),
        dataset_name="mailchimp_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _rows(pipeline, table=TABLE):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content FROM {table}") as cursor,
    ):
        return {row[0]: {"title": row[1], "content": row[2]} for row in cursor.fetchall()}


def test_source_declares_document_marker_and_its_own_pipeline():
    from cognee.tasks.ingestion.dlt_utils import (
        PIPELINE_SCOPE_ATTR,
        document_source_tag,
        pipeline_name_for_source,
    )

    a = mailchimp_source(KEY)
    b = mailchimp_source(KEY.replace("us22", "us1"))
    assert document_source_tag(a) == "mailchimp"
    assert getattr(a, PIPELINE_SCOPE_ATTR) == TABLE
    assert pipeline_name_for_source(a, "ds") != "ingest_dlt_source"
    assert pipeline_name_for_source(a, "ds") != pipeline_name_for_source(b, "ds")


def test_arguments(monkeypatch):
    # Settings come only from the arguments; the factory never reads the environment.
    monkeypatch.setenv("MAILCHIMP_API_KEY", KEY)
    with pytest.raises(ValueError, match="API key is required"):
        mailchimp_source("")
    with pytest.raises(ValueError, match="Mailchimp API key"):
        mailchimp_source("not-a-key")
    with pytest.raises(ValueError, match="max_delete_ratio"):
        mailchimp_source(KEY, max_delete_ratio=0)
    with pytest.raises(TypeError, match="list of ids"):
        mailchimp_source(KEY, audience_ids="aaaaaaaaaa")
    with pytest.raises(TypeError):
        mailchimp_source(KEY, service=httpx.Client())  # one injection point: http_client
    assert mailchimp_source(KEY, audience_ids=["aaaaaaaaaa"]).name == TABLE


def test_first_sync_loads_campaigns_and_notes(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded, include_unsent=True, include_notes=True))
    rows = _rows(pipeline)
    assert len(rows) == 3  # sent campaign, draft, note
    assert any("15 March" in r["content"] for r in rows.values())


def test_sync_stats_report_counts_only(seeded, tmp_path, campaign_named):
    pipeline = _pipeline(tmp_path)
    source = _source(seeded)
    pipeline.run(source)
    assert source.cognee_sync_stats == {
        "scanned": 1,
        "changed": 1,
        "unchanged": 0,
        "deleted": 0,
        "skipped": 0,
        "held_back": 0,
        "failed": 0,
        "failed_auth": 0,
        "failed_rate_limit": 0,
    }
    seeded.delete_campaign(campaign_named("Spring launch")["id"])
    source = _source(seeded)
    pipeline.run(source)
    assert source.cognee_sync_stats["deleted"] == 1
    assert all(isinstance(v, int) for v in source.cognee_sync_stats.values())


def test_draft_edit_replaces_the_row(seeded, tmp_path, campaign_named):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded, include_unsent=True))
    webinar = campaign_named("Webinar")
    seeded.edit_body(webinar["id"], "Join our product webinar on 27 March.")
    pipeline.run(_source(seeded, include_unsent=True))
    content = _rows(pipeline)[f"campaign:{webinar['id']}"]["content"]
    assert "27 March" in content
    assert "20 March" not in content


def test_deleted_campaign_is_removed_from_the_table(seeded, tmp_path, campaign_named):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    launch = campaign_named("Spring launch")
    seeded.delete_campaign(launch["id"])
    pipeline.run(_source(seeded))
    assert f"campaign:{launch['id']}" not in _rows(pipeline)


def test_failed_run_changes_nothing(seeded, tmp_path, campaign_named, monkeypatch):
    monkeypatch.setattr("cognee_community_connector_mailchimp.mailchimp.time.sleep", lambda s: None)
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    before = _rows(pipeline)
    launch = campaign_named("Spring launch")
    seeded.delete_campaign(launch["id"])
    seeded.fail_with = 503
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the MailchimpError
        pipeline.run(_source(seeded))
    assert _rows(pipeline) == before
    # The state did not move, so the next good run still sees the delete.
    seeded.fail_with = None
    pipeline.run(_source(seeded))
    assert f"campaign:{launch['id']}" not in _rows(pipeline)


def test_mass_delete_is_confirmed_by_the_next_run(fake, tmp_path):
    for i in range(12):
        fake.send(fake.add_campaign(f"Campaign {i}", "Body.")["id"])
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(fake))
    for cid in list(fake.campaigns)[:11]:
        fake.delete_campaign(cid)
    source = _source(fake)
    pipeline.run(source)  # no error: a scheduled sync keeps running
    assert source.cognee_sync_stats["held_back"] == 11
    assert len(_rows(pipeline)) == 12
    # The held-back set lives in dlt state, so it survives into the next run.
    pipeline.run(_source(fake))
    assert len(_rows(pipeline)) == 1


def test_failed_runs_report_a_category(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr("cognee_community_connector_mailchimp.mailchimp.time.sleep", lambda s: None)
    source = mailchimp_source("fedcba9876543210" * 2 + "-us22", http_client=seeded.http_client())
    with pytest.raises(Exception, match="API Key Invalid"):
        _pipeline(tmp_path / "a").run(source)
    assert (source.cognee_sync_stats["failed"], source.cognee_sync_stats["failed_auth"]) == (1, 1)

    seeded.fail_with = 429
    source = _source(seeded)
    with pytest.raises(Exception, match="429"):
        _pipeline(tmp_path / "b").run(source)
    stats = source.cognee_sync_stats
    assert (stats["failed"], stats["failed_rate_limit"], stats["failed_auth"]) == (1, 1, 0)


def test_check_active_can_stop_a_sync(seeded, tmp_path):
    calls = []

    def check_active():
        calls.append(1)
        if len(calls) > 3:
            raise PermissionError("credentials revoked")

    with pytest.raises(Exception, match="credentials revoked"):
        _pipeline(tmp_path).run(_source(seeded, include_unsent=True, check_active=check_active))
    assert len(calls) == 4


def test_one_resource_name_cannot_hold_two_accounts(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded, resource_name="shared"))
    original = seeded.handler

    def other_account(request):
        response = original(request)
        if request.url.path.rstrip("/") == "/3.0":
            return type(response)(200, json={"account_id": "acct2", "account_name": "Other"})
        return response

    seeded.handler = other_account
    with pytest.raises(Exception, match="own resource_name"):
        pipeline.run(_source(seeded, resource_name="shared"))


def test_two_accounts_with_their_own_resource_names_stay_apart(seeded, tmp_path, campaign_named):
    other = type(seeded)()
    other.send(other.add_campaign("Other news", "Body.")["id"])
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded, resource_name="mailchimp_a"))
    pipeline.run(_source(other, resource_name="mailchimp_b"))
    launch = campaign_named("Spring launch")
    assert set(_rows(pipeline, "mailchimp_a")) == {f"campaign:{launch['id']}"}
    assert len(_rows(pipeline, "mailchimp_b")) == 1
    # Deleting everything in one account leaves the other untouched.
    for cid in list(other.campaigns):
        other.delete_campaign(cid)
    pipeline.run(_source(other, resource_name="mailchimp_b"))
    assert _rows(pipeline, "mailchimp_b") == {}
    assert set(_rows(pipeline, "mailchimp_a")) == {f"campaign:{launch['id']}"}


def test_missing_account_id_stops_before_syncing(seeded, tmp_path):
    # The account id keys the guard against two accounts sharing one resource name.
    original = seeded.handler

    def no_account_id(request):
        if request.url.path.rstrip("/") == "/3.0":
            return httpx.Response(200, json={"account_name": "Example Workspace"})
        return original(request)

    seeded.handler = no_account_id
    with pytest.raises(Exception, match="no account id"):
        _pipeline(tmp_path).run(_source(seeded))


def test_stats_start_fresh_even_when_a_run_fails_early(seeded, tmp_path):
    source = _source(seeded, resource_name="shared")
    _pipeline(tmp_path / "a").run(source)
    assert source.cognee_sync_stats["scanned"] == 1
    # Another account owns "shared" in this pipeline, so the same source object
    # fails on the account check the second time, before reading anything.
    pipeline = _pipeline(tmp_path / "b")
    original = seeded.handler
    seeded.handler = lambda r: (
        httpx.Response(200, json={"account_id": "acct2", "account_name": "Other"})
        if r.url.path.rstrip("/") == "/3.0"
        else original(r)
    )
    pipeline.run(_source(seeded, resource_name="shared"))
    seeded.handler = original
    with pytest.raises(Exception, match="own resource_name"):
        pipeline.run(source)
    assert source.cognee_sync_stats == {
        "scanned": 0,
        "changed": 0,
        "unchanged": 0,
        "deleted": 0,
        "skipped": 0,
        "held_back": 0,
        "failed": 1,
        "failed_auth": 0,
        "failed_rate_limit": 0,
    }
