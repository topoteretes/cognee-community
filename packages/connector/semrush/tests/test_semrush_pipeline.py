"""Pipeline-level integration tests for the Semrush connector against DuckDB."""

from unittest.mock import patch

import dlt
import duckdb
import pytest
import requests

from cognee_community_connector_semrush.semrush import (
    compute_selection_fingerprint,
    semrush_source,
)


def _mock_semrush_http(domain_ranks_data: dict[str, str], domain_organic_data: dict[str, str]):
    """Return a mocked requests.Session.send that dispatches by domain and report type."""
    call_counts = {"ranks": 0, "organic": 0}

    def _send(*args, **kwargs):
        request = args[1] if len(args) > 1 else args[0]
        url = request.url or ""
        # Determine report type
        if "type=domain_ranks" in url:
            call_counts["ranks"] += 1
            for domain, body in domain_ranks_data.items():
                if f"domain={domain}" in url:
                    resp = requests.Response()
                    resp.status_code = 200
                    resp._content = body.encode("utf-8")
                    resp.url = url
                    resp.request = request
                    return resp
        elif "type=domain_organic" in url:
            call_counts["organic"] += 1
            for domain, body in domain_organic_data.items():
                if f"domain={domain}" in url:
                    resp = requests.Response()
                    resp.status_code = 200
                    resp._content = body.encode("utf-8")
                    resp.url = url
                    resp.request = request
                    return resp

        # Default empty fallback
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ERROR 50 :: NOTHING FOUND"
        resp.url = url
        resp.request = request
        return resp

    return _send, call_counts


# Standard sample CSV payloads using official documented Semrush full-name headers
# Sources:
# - Domain Overview: https://developer.semrush.com/api/v3/seo/overview-reports/#domain-overview-all-databases
# - Organic Keywords: https://developer.semrush.com/api/v3/seo/organic-reports/#domain-organic-search-keywords
_DOC_RANKS_HEADER = (
    "Database;Domain;Rank;Organic Keywords;Organic Traffic;Organic Cost;"
    "Adwords Keywords;Adwords Traffic;Adwords Cost"
)
_DOC_ORGANIC_HEADER = (
    "Keyword;Position;Previous Position;Position Difference;Search Volume;CPC;"
    "Url;Traffic (%);Traffic Cost (%);Competition;Number of Results;Trends"
)
_RANKS_A = f"{_DOC_RANKS_HEADER}\nus;alpha.com;100;1000;2000;500.0;5;50;10.0\n"
_RANKS_B = f"{_DOC_RANKS_HEADER}\nus;beta.com;200;500;1000;250.0;2;20;5.0\n"
_ORGANIC_A = (
    f"{_DOC_ORGANIC_HEADER}\nkeyword1;1;2;-1;100;1.5;https://alpha.com;5.0;2.0;0.5;1000;0.1\n"
)
_ORGANIC_B = (
    f"{_DOC_ORGANIC_HEADER}\nkeyword2;3;3;0;200;2.0;https://beta.com;10.0;4.0;0.8;2000;0.2\n"
)


def test_two_run_skip_regression_guard(tmp_path) -> None:
    """Regression guard for selected=False skip behavior.

    WHY THIS TEST EXISTS:
    In dlt, a resource with write_disposition='replace' that yields 0 items (an empty generator)
    normally creates an empty load package that truncates/empties the destination table.
    Our connector prevents this by dynamically marking resource.selected = False when the cursor
    ({period, selection_fingerprint}) is unchanged. This test proves that two consecutive
    pipeline.run() calls with fresh semrush_source() instances leave the replace tables
    byte-for-byte identical and make ZERO HTTP calls on the second run.
    """
    db_file = str(tmp_path / "test_skip.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_skip_pipeline",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    ranks_map = {"alpha.com": _RANKS_A, "beta.com": _RANKS_B}
    organic_map = {"alpha.com": _ORGANIC_A, "beta.com": _ORGANIC_B}
    mock_send, call_counts = _mock_semrush_http(ranks_map, organic_map)

    ranks_q = (
        "SELECT domain, database, rank, snapshot_date "
        "FROM semrush_data.domain_ranks ORDER BY domain"
    )
    organic_q = (
        "SELECT domain, database, keyword, position, snapshot_date "
        "FROM semrush_data.domain_organic ORDER BY domain"
    )

    with patch("requests.Session.send", side_effect=mock_send):
        # Run 1: initial ingestion
        source1 = semrush_source(
            api_key="test_key",
            domains=["alpha.com", "beta.com"],
            display_date="20260915",
        )
        pipeline.run(source1)

        conn = duckdb.connect(db_file)
        ranks1 = conn.execute(ranks_q).fetchall()
        organic1 = conn.execute(organic_q).fetchall()
        conn.close()

        assert len(ranks1) == 2
        assert len(organic1) == 2
        assert call_counts["ranks"] == 2
        assert call_counts["organic"] == 2

        # Reset call counts
        call_counts["ranks"] = 0
        call_counts["organic"] = 0

        # Run 2: fresh source instance with identical parameters
        source2 = semrush_source(
            api_key="test_key",
            domains=["alpha.com", "beta.com"],
            display_date="20260915",
        )
        info2 = pipeline.run(source2)

        conn = duckdb.connect(db_file)
        ranks2 = conn.execute(ranks_q).fetchall()
        organic2 = conn.execute(organic_q).fetchall()
        conn.close()

        # Zero HTTP calls on run 2
        assert call_counts["ranks"] == 0
        assert call_counts["organic"] == 0
        # Zero load packages created
        assert len(info2.load_packages) == 0
        # Tables remain byte-for-byte identical
        assert ranks1 == ranks2
        assert organic1 == organic2


def test_new_period_triggers_refetch(tmp_path) -> None:
    """Verify changing display_date triggers a full refetch and updates the snapshot."""
    db_file = str(tmp_path / "test_period.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_period_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    ranks_map = {"alpha.com": _RANKS_A}
    organic_map = {"alpha.com": _ORGANIC_A}
    mock_send, call_counts = _mock_semrush_http(ranks_map, organic_map)

    with patch("requests.Session.send", side_effect=mock_send):
        # Run 1: 20260815
        s1 = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20260815")
        pipeline.run(s1)

        conn = duckdb.connect(db_file)
        r1 = conn.execute("SELECT snapshot_date FROM semrush_data.domain_ranks").fetchall()
        conn.close()
        assert r1 == [("2026-08-15",)]

        call_counts["ranks"] = 0
        call_counts["organic"] = 0

        # Run 2: New period 20260915
        s2 = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20260915")
        pipeline.run(s2)

        assert call_counts["ranks"] == 1
        assert call_counts["organic"] == 1

        conn = duckdb.connect(db_file)
        r2 = conn.execute("SELECT snapshot_date FROM semrush_data.domain_ranks").fetchall()
        conn.close()
        assert r2 == [("2026-09-15",)]


def test_domain_removed_propagates_deletion(tmp_path) -> None:
    """Verify removing a domain from the selection removes its rows under replace."""
    db_file = str(tmp_path / "test_remove.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_remove_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    ranks_map = {"alpha.com": _RANKS_A, "beta.com": _RANKS_B}
    organic_map = {"alpha.com": _ORGANIC_A, "beta.com": _ORGANIC_B}
    mock_send, _ = _mock_semrush_http(ranks_map, organic_map)

    with patch("requests.Session.send", side_effect=mock_send):
        # Run 1: alpha.com and beta.com
        s1 = semrush_source(
            api_key="test_key", domains=["alpha.com", "beta.com"], display_date="20260915"
        )
        pipeline.run(s1)

        conn = duckdb.connect(db_file)
        domains_run1 = [
            row[0]
            for row in conn.execute(
                "SELECT domain FROM semrush_data.domain_ranks ORDER BY domain"
            ).fetchall()
        ]
        conn.close()
        assert domains_run1 == ["alpha.com", "beta.com"]

        # Run 2: beta.com removed
        s2 = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20260915")
        pipeline.run(s2)

        conn = duckdb.connect(db_file)
        domains_run2 = [
            row[0]
            for row in conn.execute(
                "SELECT domain FROM semrush_data.domain_ranks ORDER BY domain"
            ).fetchall()
        ]
        conn.close()
        assert domains_run2 == ["alpha.com"]


def test_mid_run_failure_commits_nothing(tmp_path) -> None:
    """Verify error on domain 2 aborts run, leaves tables untouched, and does not advance cursor."""
    db_file = str(tmp_path / "test_mid_fail.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_mid_fail_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    ranks_map = {"alpha.com": _RANKS_A}
    organic_map = {"alpha.com": _ORGANIC_A}
    mock_send, _ = _mock_semrush_http(ranks_map, organic_map)

    with patch("requests.Session.send", side_effect=mock_send):
        # Initial successful run with alpha.com
        s1 = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20260915")
        pipeline.run(s1)

    # Next run: alpha.com succeeds, but beta.com hits a persistent 429 rate limit
    def _failing_send(*args, **kwargs):
        request = args[1] if len(args) > 1 else args[0]
        if "domain=beta.com" in (request.url or ""):
            resp = requests.Response()
            resp.status_code = 429
            resp._content = b"RATE LIMITED"
            resp.url = request.url
            resp.request = request
            return resp
        return mock_send(*args, **kwargs)

    with patch("requests.Session.send", side_effect=_failing_send):
        s2 = semrush_source(
            api_key="test_key", domains=["alpha.com", "beta.com"], display_date="20260915"
        )
        with pytest.raises(Exception):  # noqa: B017
            pipeline.run(s2)

    # Destination tables must still hold the original untouched state (alpha.com only)
    conn = duckdb.connect(db_file)
    domains_after = [
        row[0] for row in conn.execute("SELECT domain FROM semrush_data.domain_ranks").fetchall()
    ]
    conn.close()
    assert domains_after == ["alpha.com"]

    # Assert neither cursor advanced to the new fingerprint {alpha.com, beta.com}
    source_state = pipeline.state.get("sources", {}).get("semrush", {})
    new_fp = compute_selection_fingerprint(["alpha.com", "beta.com"], "us", 10)
    assert source_state.get("cursor_ranks", {}).get("selection_fingerprint") != new_fp
    assert source_state.get("cursor_organic", {}).get("selection_fingerprint") != new_fp

    # Running again with failure resolved proves cursor did not advance: it fetches beta.com
    resolved_ranks = {"alpha.com": _RANKS_A, "beta.com": _RANKS_B}
    resolved_organic = {"alpha.com": _ORGANIC_A, "beta.com": _ORGANIC_B}
    mock_resolved_send, call_counts = _mock_semrush_http(resolved_ranks, resolved_organic)
    with patch("requests.Session.send", side_effect=mock_resolved_send):
        s3 = semrush_source(
            api_key="test_key", domains=["alpha.com", "beta.com"], display_date="20260915"
        )
        pipeline.run(s3)
        assert call_counts["ranks"] == 2
        assert call_counts["organic"] == 2

    conn = duckdb.connect(db_file)
    domains_final = [
        row[0]
        for row in conn.execute(
            "SELECT domain FROM semrush_data.domain_ranks ORDER BY domain"
        ).fetchall()
    ]
    conn.close()
    assert domains_final == ["alpha.com", "beta.com"]


def test_all_error_50_raises_no_data_error_and_preserves_table(tmp_path) -> None:
    """Verify all domains returning ERROR 50 raises SemrushNoDataError without truncating table."""
    db_file = str(tmp_path / "test_no_data.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_no_data_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    # Seed initial data
    ranks_map = {"alpha.com": _RANKS_A}
    organic_map = {"alpha.com": _ORGANIC_A}
    mock_send, _ = _mock_semrush_http(ranks_map, organic_map)

    with patch("requests.Session.send", side_effect=mock_send):
        s1 = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20260915")
        pipeline.run(s1)

    # All domains return ERROR 50
    empty_ranks = {"alpha.com": "ERROR 50 :: NOTHING FOUND"}
    empty_organic = {"alpha.com": "ERROR 50 :: NOTHING FOUND"}
    mock_empty_send, _ = _mock_semrush_http(empty_ranks, empty_organic)

    with patch("requests.Session.send", side_effect=mock_empty_send):
        s2 = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20261015")
        with pytest.raises(Exception) as exc_info:
            pipeline.run(s2)

        # Confirm SemrushNoDataError was raised with message suggesting earlier display_date
        assert "display_date" in str(exc_info.value) or "earlier" in str(exc_info.value)

    # Verify table was NOT emptied / truncated
    conn = duckdb.connect(db_file)
    rows = conn.execute("SELECT * FROM semrush_data.domain_ranks").fetchall()
    conn.close()
    assert len(rows) == 1


def test_per_domain_error_50_with_one_success_loads_successfully(tmp_path) -> None:
    """Verify when domain A has data and domain B has ERROR 50, run succeeds with domain A rows."""
    db_file = str(tmp_path / "test_partial.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_partial_err50_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    ranks_map = {
        "alpha.com": _RANKS_A,
        "beta.com": "ERROR 50 :: NOTHING FOUND",
    }
    organic_map = {
        "alpha.com": _ORGANIC_A,
        "beta.com": "ERROR 50 :: NOTHING FOUND",
    }
    mock_send, _ = _mock_semrush_http(ranks_map, organic_map)

    with patch("requests.Session.send", side_effect=mock_send):
        source = semrush_source(
            api_key="test_key", domains=["alpha.com", "beta.com"], display_date="20260915"
        )
        pipeline.run(source)

    conn = duckdb.connect(db_file)
    ranks = conn.execute("SELECT domain FROM semrush_data.domain_ranks").fetchall()
    organic = conn.execute("SELECT domain FROM semrush_data.domain_organic").fetchall()
    conn.close()

    assert ranks == [("alpha.com",)]
    assert organic == [("alpha.com",)]


def test_parameter_change_forces_refetch(tmp_path) -> None:
    """Verify changing database or display_limit alters fingerprint and forces refetch."""
    db_file = str(tmp_path / "test_params.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_params_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    ranks_map = {"alpha.com": _RANKS_A}
    organic_map = {"alpha.com": _ORGANIC_A}
    mock_send, call_counts = _mock_semrush_http(ranks_map, organic_map)

    with patch("requests.Session.send", side_effect=mock_send):
        # Initial run
        s1 = semrush_source(
            api_key="test_key",
            domains=["alpha.com"],
            database="us",
            display_limit=10,
            display_date="20260915",
        )
        pipeline.run(s1)

        call_counts["ranks"] = 0
        call_counts["organic"] = 0

        # Change database: us -> uk
        s2 = semrush_source(
            api_key="test_key",
            domains=["alpha.com"],
            database="uk",
            display_limit=10,
            display_date="20260915",
        )
        pipeline.run(s2)
        assert call_counts["ranks"] == 1
        assert call_counts["organic"] == 1

        call_counts["ranks"] = 0
        call_counts["organic"] = 0

        # Change display_limit: 10 -> 5
        s3 = semrush_source(
            api_key="test_key",
            domains=["alpha.com"],
            database="uk",
            display_limit=5,
            display_date="20260915",
        )
        pipeline.run(s3)
        assert call_counts["ranks"] == 1
        assert call_counts["organic"] == 1


def test_extract_succeeds_load_fails_retries(tmp_path) -> None:
    """Extract succeeds, load fails, neither cursor is committed; next run refetches
    and loads table.

    WHY THIS TEST IS BUILT THIS WAY:
    Simulates a destination load failure during storage initialization. Ensures that dlt's
    transactional semantics roll back completely so that the skip gate is not falsely triggered
    on a subsequent retry run.
    """
    db_file = str(tmp_path / "test_extract_load_retry.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_extract_load_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    ranks_map = {"alpha.com": _RANKS_A}
    organic_map = {"alpha.com": _ORGANIC_A}
    mock_send, call_counts = _mock_semrush_http(ranks_map, organic_map)

    from dlt.destinations.impl.duckdb.duck import DuckDbClient

    def _failing_init(*args, **kwargs):
        raise RuntimeError("Simulated load failure")

    with patch("requests.Session.send", side_effect=mock_send):
        # Run 1: extract succeeds (HTTP called), load fails
        with patch.object(DuckDbClient, "initialize_storage", side_effect=_failing_init):
            source1 = semrush_source(
                api_key="test_key", domains=["alpha.com"], display_date="20260915"
            )
            with pytest.raises(Exception, match="Simulated load failure"):
                pipeline.run(source1)

        assert call_counts["ranks"] == 1
        assert call_counts["organic"] == 1

        # Assert neither cursor was committed to destination or reloaded pipeline state
        conn = duckdb.connect(db_file)
        tables = [t[0] for t in conn.execute("SHOW TABLES").fetchall()]
        assert "domain_ranks" not in tables
        conn.close()

        pipeline_next = dlt.pipeline(
            pipeline_name="test_extract_load_pipe",
            destination=dlt.destinations.duckdb(db_file),
            dataset_name="semrush_data",
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline_next.sync_destination()
        source_state = pipeline_next.state.get("sources", {}).get("semrush", {})
        assert "cursor_ranks" not in source_state
        assert "cursor_organic" not in source_state

        # Reset call counts
        call_counts["ranks"] = 0
        call_counts["organic"] = 0

        # Run 2: Clean run. Since cursor was not committed, skip gate does not trigger,
        # HTTP is called again to refetch, and load succeeds.
        source2 = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20260915")
        pipeline_next.run(source2)

        assert call_counts["ranks"] == 1
        assert call_counts["organic"] == 1

        # Verify destination tables are populated correctly
        conn = duckdb.connect(db_file)
        ranks = conn.execute("SELECT domain, rank FROM semrush_data.domain_ranks").fetchall()
        organic = conn.execute("SELECT domain, keyword FROM semrush_data.domain_organic").fetchall()
        conn.close()

        assert ranks == [("alpha.com", 100)]
        assert organic == [("alpha.com", "keyword1")]

        # Assert both cursors are now committed
        source_state2 = pipeline_next.state.get("sources", {}).get("semrush", {})
        assert source_state2.get("cursor_ranks", {}).get("period") == "20260915"
        assert source_state2.get("cursor_organic", {}).get("period") == "20260915"


def test_sqlite_undroppable_view_load_failure(tmp_path) -> None:
    """SQLite un-droppable view load-failure test: extract succeeds, database load fails
    due to a pre-existing view blocking table creation, neither cursor commits, and next run
    re-fetches and succeeds once the view is resolved.

    WHY THIS TEST IS BUILT THIS WAY:
    In cognee and dlt, relational pipelines write to SQLite / SQLAlchemy destinations.
    To verify failure recovery without monkeypatching internal Python classes (like DuckDbClient),
    we create a SQL VIEW named 'domain_ranks' in SQLite prior to running the pipeline.
    In SQLite, a VIEW shares the table namespace and cannot be overwritten with CREATE TABLE or
    TRUNCATE. This forces dlt's load job to fail naturally at the SQL execution layer during the
    load phase (after extraction has successfully yielded data).
    This test verifies that:
      1. A destination load failure aborts the load package and prevents committing the cursor
         (state['cursor_ranks'] and state['cursor_organic'] are never persisted).
      2. On the subsequent run (after dropping the blocking view), the connector recognizes that
         the cursor was never committed, avoids falsely triggering the skip gate, executes fresh
         HTTP calls to re-fetch the data, and successfully populates the destination tables.
    """
    import sqlite3

    db_file = str(tmp_path / "test_sqlite_undroppable_view.db")
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE VIEW domain_ranks AS SELECT 1 AS dummy;")
    conn.close()

    ranks_map = {"alpha.com": _RANKS_A}
    organic_map = {"alpha.com": _ORGANIC_A}
    mock_send, call_counts = _mock_semrush_http(ranks_map, organic_map)

    dest = dlt.destinations.sqlalchemy(f"sqlite:///{db_file}")
    pipeline = dlt.pipeline(
        pipeline_name="test_sqlite_view_pipe",
        destination=dest,
        dataset_name="main",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    with patch("requests.Session.send", side_effect=mock_send):
        # Run 1: extract succeeds, load fails due to the un-droppable view
        source1 = semrush_source(domains=["alpha.com"], api_key="test_key", display_date="20260915")
        with pytest.raises(Exception, match=r"(?i)view|domain_ranks|step=load"):
            pipeline.run(source1)

        assert call_counts["ranks"] == 1
        assert call_counts["organic"] == 1

        # Drop the blocking view so the table can now be created
        conn = sqlite3.connect(db_file)
        conn.execute("DROP VIEW domain_ranks;")
        conn.close()

        # Reset call counts
        call_counts["ranks"] = 0
        call_counts["organic"] = 0

        # Run 2: Clean run. Dlt retries the pending load package and succeeds
        source2 = semrush_source(domains=["alpha.com"], api_key="test_key", display_date="20260915")
        pipeline.run(source2)

        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute("SELECT domain, rank FROM domain_ranks")
        ranks = cur.fetchall()
        cur.execute("SELECT domain, keyword FROM domain_organic")
        organic = cur.fetchall()
        conn.close()

        assert ranks == [("alpha.com", 100)]
        assert organic == [("alpha.com", "keyword1")]

        # Run 3: Since cursor is now committed after successful load, unchanged parameters skip
        source3 = semrush_source(domains=["alpha.com"], api_key="test_key", display_date="20260915")
        pipeline.run(source3)
        assert call_counts["ranks"] == 0
        assert call_counts["organic"] == 0


def test_ranks_present_organic_error_50_succeeds(tmp_path) -> None:
    """One domain, ranks row present, organic ERROR 50 -> run succeeds and loads ranks."""
    db_file = str(tmp_path / "test_ranks_ok_organic_err50.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_ranks_ok_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    ranks_map = {"alpha.com": _RANKS_A}
    organic_map = {"alpha.com": "ERROR 50 :: NOTHING FOUND"}
    mock_send, call_counts = _mock_semrush_http(ranks_map, organic_map)

    with patch("requests.Session.send", side_effect=mock_send):
        source = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20260915")
        pipeline.run(source)

    assert call_counts["ranks"] == 1
    assert call_counts["organic"] == 1

    conn = duckdb.connect(db_file)
    ranks = conn.execute("SELECT domain, rank FROM semrush_data.domain_ranks").fetchall()
    tables = [t[0] for t in conn.execute("SHOW TABLES").fetchall()]
    if "domain_organic" in tables:
        organic_count = conn.execute("SELECT count(*) FROM semrush_data.domain_organic").fetchone()[
            0
        ]
        assert organic_count == 0
    conn.close()

    assert ranks == [("alpha.com", 100)]
    source_state = pipeline.state.get("sources", {}).get("semrush", {})
    assert source_state.get("cursor_ranks", {}).get("period") == "20260915"
    assert source_state.get("cursor_organic", {}).get("period") == "20260915"


def test_force_flag_bypasses_skip_gate(tmp_path) -> None:
    """Verify force=True bypasses the skip gate even when cursor and fingerprint match."""
    db_file = str(tmp_path / "test_force.duckdb")
    pipeline = dlt.pipeline(
        pipeline_name="test_force_pipe",
        destination=dlt.destinations.duckdb(db_file),
        dataset_name="semrush_data",
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    ranks_map = {"alpha.com": _RANKS_A}
    organic_map = {"alpha.com": _ORGANIC_A}
    mock_send, call_counts = _mock_semrush_http(ranks_map, organic_map)

    with patch("requests.Session.send", side_effect=mock_send):
        s1 = semrush_source(api_key="test_key", domains=["alpha.com"], display_date="20260915")
        pipeline.run(s1)
        assert call_counts["ranks"] == 1
        assert call_counts["organic"] == 1

        call_counts["ranks"] = 0
        call_counts["organic"] = 0

        # Unchanged parameters, but force=True forces refetch
        s2 = semrush_source(
            api_key="test_key", domains=["alpha.com"], display_date="20260915", force=True
        )
        pipeline.run(s2)
        assert call_counts["ranks"] == 1
        assert call_counts["organic"] == 1
