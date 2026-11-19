"""sanity_source(): configuration, the dlt resource it builds, and a real dlt merge."""

import dlt
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_sanity import FakeSanity, block

from cognee_community_connector_sanity import sanity_source


@pytest.fixture(autouse=True)
def no_active_pipeline():
    """Iterating a resource outside a run uses the active pipeline's state, so make
    sure a pipeline left active by an earlier test cannot leak its state in."""
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()


def test_resource_is_a_document_source_scoped_per_project_and_dataset():
    resource = sanity_source("abc123", "production", ["article"], client=FakeSanity())
    assert resource.name == "sanity_abc123_production"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "sanity"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "sanity_abc123_production"
    assert sanity_source("abc123", "staging", ["article"], client=FakeSanity()).name == (
        "sanity_abc123_staging"
    )


def test_settings_are_arguments_only_and_the_environment_is_not_read(monkeypatch):
    monkeypatch.setenv("SANITY_PROJECT_ID", "env123")
    monkeypatch.setenv("SANITY_DOCUMENT_TYPES", "article")
    monkeypatch.setenv("SANITY_TOKEN", "sk-from-env")
    with pytest.raises(ValueError, match="project_id"):
        sanity_source(client=FakeSanity())
    resource = sanity_source("p1", document_types=["article"])
    assert resource.name == "sanity_p1_production", "the dataset defaults to production"


def test_service_is_an_alias_for_client():
    fake = FakeSanity()
    fake.put("a1", "article", title="One")
    rows = list(sanity_source("p1", "production", ["article"], service=fake))
    assert [r["title"] for r in rows] == ["One"]
    with pytest.raises(ValueError, match="not both"):
        sanity_source("p1", "production", ["article"], client=fake, service=FakeSanity())


def test_a_rejected_token_is_counted_and_raised():
    from cognee_community_connector_sanity import SanityAuthError

    fake = FakeSanity()
    fake.fail["list_revisions"] = SanityAuthError("HTTP 401")
    resource = sanity_source("p1", "production", ["article"], client=fake)
    with pytest.raises(Exception) as info:
        list(resource)
    chain, error = [], info.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(e, SanityAuthError) for e in chain)
    assert resource.cognee_sync_stats["failed_auth"] == 1


def test_a_lasting_rate_limit_ends_the_run_cleanly_and_reports_it():
    from cognee_community_connector_sanity.sanity import SanityRateLimitError

    fake = FakeSanity()
    fake.put("a1", "article", title="One")
    fake.fail["get_documents"] = SanityRateLimitError("429")
    resource = sanity_source("p1", "production", ["article"], client=fake)
    assert list(resource) == [], "no rows, no tombstones, no state change"
    assert resource.cognee_sync_stats["failed_rate_limit"] == 1
    assert all(type(v) is int for v in resource.cognee_sync_stats.values())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"project_id": "", "document_types": ["a"]}, "project_id"),
        ({"project_id": "Bad.Id", "document_types": ["a"]}, "project_id"),
        ({"project_id": "p1", "dataset": "../x", "document_types": ["a"]}, "dataset"),
        ({"project_id": "p1", "document_types": []}, "document_types is empty"),
        ({"project_id": "p1", "document_types": ["ok", "no spaces"]}, "Not valid"),
        ({"project_id": "p1", "document_types": ["a"], "api_version": "2025"}, "API version"),
        (
            {"project_id": "p1", "document_types": ["a"], "resource_name": "My-Site"},
            "resource_name",
        ),
        ({"project_id": "p1", "document_types": ["a"], "resource_name": "mySite"}, "resource_name"),
        ({"project_id": "p1", "document_types": ["a"], "resource_name": ""}, "resource_name"),
    ],
)
def test_bad_configuration_fails_when_the_source_is_built(kwargs, message):
    with pytest.raises(ValueError, match=message):
        sanity_source(client=FakeSanity(), **kwargs)


def _pipeline(tmp_path, name):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / name}.db"),
        pipelines_dir=str(tmp_path / "pipelines"),
        dataset_name="sanity",
    )


def _rows(pipeline, table):
    with pipeline.sql_client() as client:
        return {
            row[0]: row[1]
            for row in client.execute_sql(f"SELECT id, title FROM {table} ORDER BY id")
        }


def test_a_real_dlt_merge_upserts_and_hard_deletes(tmp_path):
    fake = FakeSanity()
    fake.put("a1", "article", title="First", body=[block("one")])
    fake.put("a2", "article", title="Second", body=[block("two")])
    pipeline = _pipeline(tmp_path, "sanity_merge")
    table = "sanity_p1_production"

    pipeline.run(sanity_source("p1", "production", ["article"], client=fake))
    assert _rows(pipeline, table) == {
        "sanity:p1:production:a1": "First",
        "sanity:p1:production:a2": "Second",
    }

    fake.patch("a1", title="First, edited")
    fake.delete("a2")
    pipeline.run(sanity_source("p1", "production", ["article"], client=fake))

    assert _rows(pipeline, table) == {"sanity:p1:production:a1": "First, edited"}


def test_sync_stats_hold_counts_only(tmp_path):
    fake = FakeSanity()
    fake.put("a1", "article", title="Secret launch plan")
    resource = sanity_source("p1", "production", ["article"], client=fake)
    _pipeline(tmp_path, "sanity_stats").run(resource)
    assert resource.cognee_sync_stats["changed"] == 1
    assert all(isinstance(v, int) for v in resource.cognee_sync_stats.values())


def test_check_active_stops_the_sync_between_rows_and_nothing_is_saved(tmp_path):
    fake = FakeSanity()
    for i in range(3):
        fake.put(f"a{i}", "article", title=f"Doc {i}")
    pipeline = _pipeline(tmp_path, "sanity_revoked")
    calls = {"n": 0}

    def check_active():
        calls["n"] += 1
        if calls["n"] > 3:  # a few checks in: after the requests, between rows
            raise PermissionError("access revoked")

    resource = sanity_source(
        "p1", "production", ["article"], client=fake, check_active=check_active
    )
    with pytest.raises(Exception, match="access revoked"):
        pipeline.run(resource)

    # Nothing was loaded and no state was saved, so the next good run syncs everything.
    resource = sanity_source("p1", "production", ["article"], client=fake)
    pipeline.run(resource)
    assert resource.cognee_sync_stats["changed"] == 3
    assert len(_rows(pipeline, "sanity_p1_production")) == 3


def test_two_datasets_in_one_pipeline_stay_apart(tmp_path):
    production, staging = FakeSanity(), FakeSanity()
    production.put("a1", "article", title="Live")
    staging.put("a1", "article", title="Draft copy")
    pipeline = _pipeline(tmp_path, "sanity_two")

    pipeline.run(sanity_source("p1", "production", ["article"], client=production))
    pipeline.run(sanity_source("p1", "staging", ["article"], client=staging))
    staging.put("a2", "article", title="Keeps staging listed")
    staging.delete("a1")
    pipeline.run(sanity_source("p1", "staging", ["article"], client=staging))

    assert _rows(pipeline, "sanity_p1_production") == {"sanity:p1:production:a1": "Live"}
    assert _rows(pipeline, "sanity_p1_staging") == {"sanity:p1:staging:a2": "Keeps staging listed"}
