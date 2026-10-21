"""ahrefs_source() factory: validation, cost budget, markers, and a real dlt run."""

from datetime import date

import dlt
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_ahrefs import FakeAhrefs

import cognee_community_connector_ahrefs.ahrefs as module
from cognee_community_connector_ahrefs import ahrefs_source

TARGET = "example.com"


@pytest.fixture(autouse=True)
def no_ahrefs_env(monkeypatch):
    monkeypatch.delenv("AHREFS_API_KEY", raising=False)
    monkeypatch.setattr(module, "_today", lambda: date(2026, 10, 15))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def test_one_source_per_report_on_the_relational_path():
    sources = ahrefs_source(
        TARGET,
        reports=["domain_rating", "backlinks", "organic_keywords", "site_audit"],
        project_ids=42,
        api_key="test-key",
    )

    assert [s.name for s in sources] == [
        "ahrefs_domain_rating",
        "ahrefs_backlinks",
        "ahrefs_organic_keywords",
        "ahrefs_site_audit",
    ]
    for source in sources:
        # Structured rows: no document marker, so cognee takes the relational path.
        assert getattr(source, DOCUMENT_SOURCE_ATTR, None) is None
        assert getattr(source, PIPELINE_SCOPE_ATTR) == source.name
        assert source.cognee_sync_stats == {}
        (resource,) = source.resources.values()
        assert resource.write_disposition == "merge"


def test_domain_rating_is_the_default_report():
    (source,) = ahrefs_source([TARGET], api_key="test-key")

    assert source.name == "ahrefs_domain_rating"


def test_name_prefixes_the_tables_for_a_second_account():
    (source,) = ahrefs_source([TARGET], api_key="test-key", name="client_b")

    assert source.name == "client_b_domain_rating"


def test_api_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("AHREFS_API_KEY", "from-env")

    assert ahrefs_source([TARGET])


def test_a_missing_api_key_fails_when_built():
    with pytest.raises(ValueError, match="AHREFS_API_KEY"):
        ahrefs_source([TARGET])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"targets": []}, "targets is empty"),
        ({"targets": [" "]}, "targets is empty"),
        ({"reports": ["traffic"]}, "Unknown Ahrefs report 'traffic'"),
        ({"reports": []}, "reports is empty"),
        ({"reports": ["site_audit"]}, "needs project_ids"),
        ({"mode": "everything"}, "mode must be one of"),
        ({"history_grouping": "yearly"}, "history_grouping must be one of"),
        ({"backlinks_limit": 0}, "backlinks_limit must be an integer"),
        ({"keep_snapshots": 0}, "keep_snapshots must be an integer"),
        ({"name": "Bad-Name"}, "name must start with a letter"),
    ],
)
def test_bad_arguments_are_refused(kwargs, message):
    arguments = {"targets": [TARGET], "api_key": "test-key", **kwargs}
    with pytest.raises(ValueError, match=message):
        ahrefs_source(**arguments)


def test_targets_are_cleaned_and_deduplicated(monkeypatch):
    configs = []
    monkeypatch.setattr(module, "_report_source", lambda *args: configs.append(args[3]))

    ahrefs_source([" example.com", "example.com", "b.test "], api_key="test-key")

    assert configs[0].targets == ("example.com", "b.test")


# ---------------------------------------------------------------------------
# Cost budget
# ---------------------------------------------------------------------------
def test_a_sync_over_max_units_fails_before_any_request():
    with pytest.raises(ValueError, match=r"up to 1300 Ahrefs API units .*max_units=1000"):
        ahrefs_source(
            [TARGET, "b.test"],
            reports=["domain_rating", "backlinks", "organic_keywords"],
            api_key="test-key",
        )


def test_raising_max_units_allows_the_sync():
    sources = ahrefs_source(
        [TARGET, "b.test"],
        reports=["domain_rating", "backlinks", "organic_keywords"],
        api_key="test-key",
        max_units=2000,
    )

    assert len(sources) == 3


def test_estimates_follow_the_ahrefs_cost_formula():
    cfg = module._Config(
        targets=(TARGET,),
        project_ids=("1", "2"),
        mode="subdomains",
        history_months=12,
        history_grouping="daily",
        backlinks_limit=100,
        keywords_limit=10,
        country=None,
        lost_lookback_days=90,
        keep_snapshots=12,
    )

    # 2 fields x 373 daily points.
    assert module._estimate("domain_rating", cfg) == 746
    # 8 fields x 100 rows.
    assert module._estimate("backlinks", cfg) == 800
    # 4 fields x 10 rows is below the 50-unit minimum.
    assert module._estimate("organic_keywords", cfg) == 50
    # A fixed 50 units per project.
    assert module._estimate("site_audit", cfg) == 100


@pytest.mark.parametrize(
    ("target", "free"),
    [
        ("wordcount.com", True),
        ("https://ahrefs.com/blog/", True),
        ("blog.ahrefs.com", True),
        ("notahrefs.com", False),
        ("example.com", False),
    ],
)
def test_free_test_targets(target, free):
    assert module._is_free(target) is free


# ---------------------------------------------------------------------------
# A real dlt run: merge, hard delete and state across runs
# ---------------------------------------------------------------------------
def _link(n, **extra):
    return {
        "url_from": f"https://blog{n}.test/post",
        "url_to": f"https://{TARGET}/",
        "anchor": "seo",
        "domain_rating_source": 40.0 + n,
        "is_dofollow": True,
        "first_seen_link": "2026-01-02T03:04:05Z",
        "last_visited": "2026-10-01T00:00:00Z",
        "is_lost": False,
        **extra,
    }


def _staged(pipeline):
    with pipeline.sql_client() as client:
        return sorted(row[0] for row in client.execute_sql("SELECT url_from FROM ahrefs_backlinks"))


def test_dlt_run_forgets_a_lost_link_and_keeps_rows_on_a_failed_run(tmp_path):
    api = FakeAhrefs()
    api.backlinks[TARGET] = [_link(1), _link(2)]
    pipeline = dlt.pipeline(
        pipeline_name="ahrefs_test",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'staging.db'}"),
        dataset_name="seo",
    )

    def run():
        (source,) = ahrefs_source(
            [TARGET], reports="backlinks", api_key="test-key", session=api.session()
        )
        pipeline.run(source, write_disposition="merge", primary_key="id")
        return source

    source = run()
    assert _staged(pipeline) == ["https://blog1.test/post", "https://blog2.test/post"]
    assert source.cognee_sync_stats["rows"] == 2

    # A failed run changes neither the rows nor the state.
    api.backlinks[TARGET] = [_link(1), _link(2, is_lost=True, last_seen="2026-10-10")]
    api.fail["site-explorer/all-backlinks"] = 500
    with pytest.raises(PipelineStepFailed) as caught:
        run()
    assert module._http_status(caught.value) == 500
    assert _staged(pipeline) == ["https://blog1.test/post", "https://blog2.test/post"]

    # The next good run forgets the lost link.
    del api.fail["site-explorer/all-backlinks"]
    source = run()
    assert _staged(pipeline) == ["https://blog1.test/post"]
    assert source.cognee_sync_stats["deleted"] == 1
