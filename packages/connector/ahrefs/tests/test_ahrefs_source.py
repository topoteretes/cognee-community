"""ahrefs_source() factory: validation, cost budget, markers, and real dlt runs."""

import random
from datetime import date

import dlt
import pytest
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    pipeline_name_for_source,
)
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_ahrefs import API_KEY, FakeAhrefs

import cognee_community_connector_ahrefs.ahrefs as module
from cognee_community_connector_ahrefs import ahrefs_source

TARGET = "example.com"


@pytest.fixture(autouse=True)
def no_ahrefs_env(monkeypatch):
    monkeypatch.delenv("AHREFS_API_KEY", raising=False)
    monkeypatch.setattr(module, "_today", lambda: date(2026, 10, 15))
    monkeypatch.setattr(module, "_sleep", lambda _seconds: None)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def test_one_source_per_report_on_the_relational_path():
    sources = ahrefs_source(
        TARGET,
        reports=["domain_rating", "backlinks", "organic_keywords", "site_audit"],
        project_ids=42,
        api_key=API_KEY,
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
        (resource,) = source.resources.values()
        # One name for the source, the resource, the table and the pipeline scope.
        assert resource.name == source.name
        assert getattr(source, PIPELINE_SCOPE_ATTR) == source.name
        assert resource.write_disposition == "merge"
        # Counts only, ready before the first run, on the source and the resource.
        assert resource.cognee_sync_stats is source.cognee_sync_stats
        assert all(isinstance(v, int) for v in source.cognee_sync_stats.values())
        assert source.cognee_sync_stats["failed_auth"] == 0


def test_domain_rating_is_the_default_report():
    (source,) = ahrefs_source([TARGET], api_key=API_KEY)

    assert source.name == "ahrefs_domain_rating"


def test_resource_name_prefixes_every_name_for_a_second_account():
    (source,) = ahrefs_source([TARGET], api_key=API_KEY, resource_name="client_b")

    assert source.name == "client_b_domain_rating"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "client_b_domain_rating"


def test_api_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("AHREFS_API_KEY", "from-env")

    assert ahrefs_source([TARGET])


def test_a_missing_api_key_fails_when_built():
    with pytest.raises(ValueError, match="AHREFS_API_KEY"):
        ahrefs_source([TARGET])


def test_service_is_an_alias_for_session():
    api = FakeAhrefs()
    session = api.session()

    assert ahrefs_source([TARGET], api_key=API_KEY, service=session)
    assert ahrefs_source([TARGET], api_key=API_KEY, session=session, service=session)
    with pytest.raises(ValueError, match="not both"):
        ahrefs_source([TARGET], api_key=API_KEY, session=session, service=api.session())


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
        ({"backlinks_limit": True}, "backlinks_limit must be an integer"),
        ({"keywords_limit": "50"}, "keywords_limit must be an integer"),
        ({"keep_snapshots": 0}, "keep_snapshots must be an integer"),
        ({"max_units": -1}, "max_units must be an integer"),
        ({"resource_name": ""}, "resource_name must start with a letter"),
        ({"resource_name": "   "}, "resource_name must start with a letter"),
        ({"resource_name": "Client_B"}, "resource_name must start with a letter"),
        ({"resource_name": "client-b"}, "resource_name must start with a letter"),
        ({"resource_name": "a" * 41}, "at most 40 characters"),
        ({"resource_name": None}, "resource_name must start with a letter"),
    ],
)
def test_bad_arguments_are_refused(kwargs, message):
    arguments = {"targets": [TARGET], "api_key": API_KEY, **kwargs}
    with pytest.raises(ValueError, match=message):
        ahrefs_source(**arguments)


def test_targets_are_cleaned_and_deduplicated(monkeypatch):
    configs = []
    monkeypatch.setattr(module, "_report_source", lambda *args: configs.append(args[3]))

    ahrefs_source([" example.com", "example.com", "b.test "], api_key=API_KEY)

    assert configs[0].targets == ("example.com", "b.test")


# ---------------------------------------------------------------------------
# Cost budget
# ---------------------------------------------------------------------------
def test_a_sync_over_max_units_fails_before_any_request():
    with pytest.raises(ValueError, match=r"up to 1300 Ahrefs API units .*max_units=1000"):
        ahrefs_source(
            [TARGET, "b.test"],
            reports=["domain_rating", "backlinks", "organic_keywords"],
            api_key=API_KEY,
        )


def test_raising_max_units_allows_the_sync():
    sources = ahrefs_source(
        [TARGET, "b.test"],
        reports=["domain_rating", "backlinks", "organic_keywords"],
        api_key=API_KEY,
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
        ("ahrefs.com.evil.test", False),
        ("example.com", False),
    ],
)
def test_free_test_targets(target, free):
    assert module._is_free(target) is free


# ---------------------------------------------------------------------------
# Real dlt runs: merge, hard delete and state across runs
# ---------------------------------------------------------------------------
def _link(n, target=TARGET, **extra):
    return {
        "url_from": f"https://blog{n}.test/post",
        "url_to": f"https://{target}/",
        "anchor": "seo",
        "domain_rating_source": 40.0 + n % 60,
        "is_dofollow": True,
        "first_seen_link": "2026-01-02T03:04:05Z",
        "last_visited": "2026-10-01T00:00:00Z",
        "is_lost": False,
        **extra,
    }


def _pipeline(tmp_path, name="ahrefs_test"):
    return dlt.pipeline(
        pipeline_name=name,
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'staging.db'}"),
        dataset_name="seo",
    )


def _staged(pipeline, table="ahrefs_backlinks"):
    with pipeline.sql_client() as client:
        return sorted(row[0] for row in client.execute_sql(f"SELECT url_from FROM {table}"))


def test_dlt_run_forgets_a_lost_link_and_keeps_rows_on_a_failed_run(tmp_path):
    api = FakeAhrefs()
    api.backlinks[TARGET] = [_link(1), _link(2)]
    pipeline = _pipeline(tmp_path)

    def run():
        (source,) = ahrefs_source(
            [TARGET], reports="backlinks", api_key=API_KEY, session=api.session()
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
    assert _cause(caught.value, module.AhrefsAPIError).status == 500
    assert _staged(pipeline) == ["https://blog1.test/post", "https://blog2.test/post"]

    # The next good run forgets the lost link.
    del api.fail["site-explorer/all-backlinks"]
    source = run()
    assert _staged(pipeline) == ["https://blog1.test/post"]
    assert source.cognee_sync_stats["deleted"] == 1


def test_two_accounts_in_one_dataset_keep_their_own_rows_and_state(tmp_path):
    api = FakeAhrefs()
    api.backlinks["a.test"] = [_link(1, "a.test")]
    api.backlinks["b.test"] = [_link(2, "b.test")]

    def run(resource_name, target):
        (source,) = ahrefs_source(
            [target],
            reports="backlinks",
            api_key=API_KEY,
            session=api.session(),
            resource_name=resource_name,
        )
        # The pipeline name cognee picks for this source in dataset "seo".
        pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "seo"))
        pipeline.run(source, write_disposition="merge", primary_key="id")
        return pipeline

    first = run("account_a", "a.test")
    second = run("account_b", "b.test")
    assert first.pipeline_name != second.pipeline_name

    # A's next sync must not forget B's link as a removed target.
    first = run("account_a", "a.test")
    assert _staged(first, "account_a_backlinks") == ["https://blog1.test/post"]
    assert _staged(second, "account_b_backlinks") == ["https://blog2.test/post"]


def test_many_syncs_of_many_links_keep_staging_equal_to_the_model(tmp_path):
    """2,000 links over 6 syncs through a real dlt merge, with links lost each time."""
    rng = random.Random(7)
    api = FakeAhrefs()
    links = {n: _link(n) for n in range(2000)}
    pipeline = _pipeline(tmp_path)
    expected: set[str] = set()

    for run in range(6):
        live = [n for n in links if not links[n]["is_lost"]]
        for n in rng.sample(live, 150):
            links[n] = {**links[n], "is_lost": True, "last_seen": "2026-10-14"}
        api.backlinks[TARGET] = list(links.values())
        (source,) = ahrefs_source(
            [TARGET],
            reports="backlinks",
            api_key=API_KEY,
            session=api.session(),
            backlinks_limit=2000,
            max_units=20_000,
        )
        pipeline.run(source, write_disposition="merge", primary_key="id")

        for row in api.last_backlinks:
            (expected.discard if row["is_lost"] else expected.add)(row["url_from"])
        assert _staged(pipeline) == sorted(expected), f"run {run}"
    assert len(expected) == 2000 - 6 * 150


def _cause(exc: BaseException, kind: type) -> BaseException:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, kind):
            return current
        current = current.__cause__ or current.__context__
    raise AssertionError(f"no {kind.__name__} in {exc!r}")
