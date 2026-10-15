"""Unit tests for the browser history & bookmarks connector."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cognee_community_connector_browser.browser import (
    BROWSER_SOURCE_NAME,
    BROWSER_TABLE_NAME,
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    PIPELINE_SCOPE_ATTR,
    _CHROMIUM_EPOCH_DIFF_US,
    browser_source,
    chromium_us_to_datetime,
    copy_browser_db,
    datetime_to_chromium_us,
    datetime_to_firefox_us,
    detect_browser_profile,
    firefox_us_to_datetime,
    generate_row_id,
    is_search_results_url,
    is_url_allowed,
    normalize_and_redact_url,
    parse_chromium_bookmarks,
    parse_firefox_bookmarks,
    sync_browser_pages,
)


# ---------------------------------------------------------------------------
# Fixtures for Chromium & Firefox mock profiles
# ---------------------------------------------------------------------------


def make_chromium_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE urls (
            id INTEGER PRIMARY KEY,
            url LONGVARCHAR,
            title LONGVARCHAR,
            visit_count INTEGER DEFAULT 0 NOT NULL,
            typed_count INTEGER DEFAULT 0 NOT NULL,
            last_visit_time INTEGER NOT NULL,
            hidden INTEGER DEFAULT 0 NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE visits (
            id INTEGER PRIMARY KEY,
            url INTEGER NOT NULL,
            visit_time INTEGER NOT NULL,
            from_visit INTEGER,
            transition INTEGER DEFAULT 0 NOT NULL,
            visit_duration INTEGER DEFAULT 0 NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def make_firefox_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE moz_places (
            id INTEGER PRIMARY KEY,
            url LONGVARCHAR,
            title LONGVARCHAR,
            rev_host LONGVARCHAR,
            visit_count INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0,
            typed INTEGER DEFAULT 0,
            frecency INTEGER DEFAULT -1,
            last_visit_date INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE moz_historyvisits (
            id INTEGER PRIMARY KEY,
            from_visit INTEGER,
            place_id INTEGER,
            visit_date INTEGER,
            visit_type INTEGER,
            session INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE moz_bookmarks (
            id INTEGER PRIMARY KEY,
            type INTEGER,
            fk INTEGER,
            parent INTEGER,
            position INTEGER,
            title LONGVARCHAR,
            keyword_id INTEGER,
            folder_type TEXT,
            dateAdded INTEGER,
            lastModified INTEGER,
            guid TEXT
        )
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Epoch conversion tests
# ---------------------------------------------------------------------------


def test_chromium_epoch_conversion():
    dt = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    us = datetime_to_chromium_us(dt)
    assert us > _CHROMIUM_EPOCH_DIFF_US

    converted_dt = chromium_us_to_datetime(us)
    assert converted_dt == dt


def test_firefox_epoch_conversion():
    dt = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    us = datetime_to_firefox_us(dt)
    assert us > 0

    converted_dt = firefox_us_to_datetime(us)
    assert converted_dt == dt


def test_epochs_do_not_mix():
    # Mixing up epochs: a Firefox timestamp in Chromium epoch yields year ~1650
    dt_2026 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ff_us = datetime_to_firefox_us(dt_2026)
    mistaken_dt = chromium_us_to_datetime(ff_us)
    assert mistaken_dt.year < 1700


# ---------------------------------------------------------------------------
# URL normalization & redaction tests
# ---------------------------------------------------------------------------


def test_normalize_and_redact_url():
    # Drops fragment and sensitive/tracking query params
    url = "https://docs.python.org/3/library/sqlite3.html?utm_source=twitter&token=secret123&page=1#anchor"
    normalized = normalize_and_redact_url(url)
    assert normalized == "https://docs.python.org/3/library/sqlite3.html?page=1"

    # Redacts auth codes and session keys
    auth_url = "https://example.com/callback?code=AUTHCODE1234&state=xyz987"
    assert normalize_and_redact_url(auth_url) == "https://example.com/callback"

    # Strips default ports
    assert normalize_and_redact_url("http://example.com:80/path") == "http://example.com/path"
    assert normalize_and_redact_url("https://example.com:443/path") == "https://example.com/path"

    # Drops URLs with embedded credentials
    assert normalize_and_redact_url("https://user:pass@example.com/path") is None


def test_is_url_allowed_privacy():
    # Allowed documentation / blog URL
    assert is_url_allowed("https://docs.python.org/3/library/sqlite3.html") is True

    # Excluded schemes
    assert is_url_allowed("chrome://settings") is False
    assert is_url_allowed("about:blank") is False
    assert is_url_allowed("file:///etc/passwd") is False
    assert is_url_allowed("chrome-extension://abcdef/popup.html") is False

    # Excluded localhost and private network
    assert is_url_allowed("http://localhost:8080/dashboard") is False
    assert is_url_allowed("http://127.0.0.1:3000") is False
    assert is_url_allowed("http://app.internal/secret") is False

    # Excluded sensitive domains (banking & webmail)
    assert is_url_allowed("https://chase.com/myaccount") is False
    assert is_url_allowed("https://mail.google.com/mail/u/0/#inbox") is False
    assert is_url_allowed("https://accounts.google.com/signin") is False
    assert is_url_allowed("https://paypal.com/signin") is False

    # Excluded sensitive endpoints/paths
    assert is_url_allowed("https://example.com/login") is False
    assert is_url_allowed("https://example.com/auth/callback") is False
    assert is_url_allowed("https://example.com/user/password_reset") is False
    assert is_url_allowed("https://example.com/cart/checkout") is False


def test_is_search_results_dropped():
    from urllib.parse import urlparse

    assert is_search_results_url(urlparse("https://www.google.com/search?q=postgres+vacuum")) is True
    assert is_search_results_url(urlparse("https://bing.com/search?q=rust")) is True
    assert is_search_results_url(urlparse("https://duckduckgo.com/?q=cognee")) is True

    # Actual destination page is NOT a search results page
    assert is_search_results_url(urlparse("https://www.postgresql.org/docs/16/routine-vacuuming.html")) is False


# ---------------------------------------------------------------------------
# Chromium History & Bookmarks parsing
# ---------------------------------------------------------------------------


def test_chromium_sync(tmp_path):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt1 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 10, 2, 11, 0, 0, tzinfo=timezone.utc)
    us1 = datetime_to_chromium_us(dt1)
    us2 = datetime_to_chromium_us(dt2)

    conn = sqlite3.connect(history_file)
    conn.execute(
        "INSERT INTO urls VALUES (1, 'https://docs.python.org/3/', 'Python Docs', 5, 0, ?, 0)",
        (us1,),
    )
    conn.execute("INSERT INTO visits VALUES (101, 1, ?, 0, 0, 0)", (us1,))

    conn.execute(
        "INSERT INTO urls VALUES (2, 'https://github.com/topoteretes/cognee', 'cognee repo', 3, 0, ?, 0)",
        (us2,),
    )
    conn.execute("INSERT INTO visits VALUES (102, 2, ?, 0, 0, 0)", (us2,))
    conn.commit()
    conn.close()

    # Bookmarks JSON
    bookmarks_file = tmp_path / "Bookmarks"
    bookmarks_content = {
        "roots": {
            "bookmark_bar": {
                "name": "Bookmarks bar",
                "type": "folder",
                "children": [
                    {
                        "name": "Dev",
                        "type": "folder",
                        "children": [
                            {
                                "date_added": str(us1),
                                "name": "Python Docs",
                                "type": "url",
                                "url": "https://docs.python.org/3/",
                            }
                        ],
                    }
                ],
            }
        }
    }
    bookmarks_file.write_text(json.dumps(bookmarks_content), encoding="utf-8")

    resource_state = {}
    rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state=resource_state,
            profile_name="Default",
        )
    )

    assert len(rows) == 2
    assert all(r["_deleted"] is False for r in rows)

    # Check python docs row
    py_row = next(r for r in rows if "docs.python.org" in r["url"])
    assert py_row["visit_count"] == 5
    assert py_row["is_bookmarked"] is True
    assert py_row["bookmark_folder"] == "Bookmarks bar/Dev"
    assert "browser:chrome" in py_row[NODE_SET_COLUMN]
    assert "browser:docs.python.org" in py_row[NODE_SET_COLUMN]
    assert "browser:bookmarks/Bookmarks bar/Dev" in py_row[NODE_SET_COLUMN]

    # Check state update
    assert resource_state["last_visit_time"] == us2
    assert len(resource_state["known_ids"]) == 2


# ---------------------------------------------------------------------------
# Incremental sync & forget-on-delete
# ---------------------------------------------------------------------------


def test_incremental_sync_and_forget_on_delete(tmp_path):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    dt2 = datetime(2026, 10, 2, tzinfo=timezone.utc)
    us1 = datetime_to_chromium_us(dt1)
    us2 = datetime_to_chromium_us(dt2)

    conn = sqlite3.connect(history_file)
    conn.execute(
        "INSERT INTO urls VALUES (1, 'https://docs.python.org/3/', 'Python Docs', 2, 0, ?, 0)",
        (us1,),
    )
    conn.execute("INSERT INTO visits VALUES (101, 1, ?, 0, 0, 0)", (us1,))
    conn.execute(
        "INSERT INTO urls VALUES (2, 'https://crates.io/', 'Crates', 1, 0, ?, 0)",
        (us2,),
    )
    conn.execute("INSERT INTO visits VALUES (102, 2, ?, 0, 0, 0)", (us2,))
    conn.commit()
    conn.close()

    # Initial sync
    state = {}
    run1_rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state=state,
        )
    )
    assert len(run1_rows) == 2
    crates_id = generate_row_id("chrome", "default", "https://crates.io/")

    # Run 2: User cleared crates.io from history, and visited a new page
    dt3 = datetime(2026, 10, 3, tzinfo=timezone.utc)
    us3 = datetime_to_chromium_us(dt3)

    conn = sqlite3.connect(history_file)
    conn.execute("DELETE FROM urls WHERE id = 2")
    conn.execute("DELETE FROM visits WHERE url = 2")
    conn.execute(
        "INSERT INTO urls VALUES (3, 'https://docs.rs/tokio', 'Tokio', 1, 0, ?, 0)",
        (us3,),
    )
    conn.execute("INSERT INTO visits VALUES (103, 3, ?, 0, 0, 0)", (us3,))
    conn.commit()
    conn.close()

    run2_rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state=state,
        )
    )

    # Should emit 1 tombstone for crates.io and 1 new row for tokio.
    # Python Docs was NOT re-visited so cursor skips re-emitting it!
    tombstones = [r for r in run2_rows if r.get("_deleted") is True]
    active_rows = [r for r in run2_rows if r.get("_deleted") is False]

    assert len(tombstones) == 1
    assert tombstones[0]["id"] == crates_id

    assert len(active_rows) == 1
    assert active_rows[0]["url"] == "https://docs.rs/tokio"

    # Known ids in state should now reflect only active pages
    assert crates_id not in state["known_ids"]
    assert state["last_visit_time"] == us3


def test_exclude_domains_causes_deletion(tmp_path):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    us1 = datetime_to_chromium_us(dt1)

    conn = sqlite3.connect(history_file)
    conn.execute(
        "INSERT INTO urls VALUES (1, 'https://docs.python.org/3/', 'Python Docs', 2, 0, ?, 0)",
        (us1,),
    )
    conn.execute(
        "INSERT INTO urls VALUES (2, 'https://news.ycombinator.com/', 'Hacker News', 1, 0, ?, 0)",
        (us1,),
    )
    conn.commit()
    conn.close()

    state = {}
    run1_rows = list(
        sync_browser_pages(browser="chrome", profile_path=tmp_path, resource_state=state)
    )
    assert len(run1_rows) == 2

    # Run 2: User adds ycombinator.com to exclude_domains
    hn_id = generate_row_id("chrome", "default", "https://news.ycombinator.com/")
    run2_rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state=state,
            exclude_domains=["ycombinator.com"],
        )
    )

    # HN must be tombstoned
    tombstones = [r for r in run2_rows if r.get("_deleted") is True]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == hn_id


# ---------------------------------------------------------------------------
# Firefox places.sqlite tests
# ---------------------------------------------------------------------------


def test_firefox_sync(tmp_path):
    places_file = tmp_path / "places.sqlite"
    make_firefox_db(places_file)

    dt = datetime(2026, 10, 10, 8, 30, 0, tzinfo=timezone.utc)
    us = datetime_to_firefox_us(dt)

    conn = sqlite3.connect(places_file)
    # moz_bookmarks folder hierarchy: toolbar (parent 1, id 2) -> Rust (parent 2, id 10)
    conn.execute("INSERT INTO moz_bookmarks VALUES (2, 2, NULL, 1, 0, 'Bookmarks Toolbar', NULL, NULL, 0, 0, 'toolbar')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (10, 2, NULL, 2, 0, 'Rust', NULL, NULL, 0, 0, 'rust')")

    # moz_places
    conn.execute(
        "INSERT INTO moz_places VALUES (1, 'https://www.rust-lang.org/', 'Rust Language', '', 10, 0, 1, 100, ?)",
        (us,),
    )
    conn.execute("INSERT INTO moz_historyvisits VALUES (101, 0, 1, ?, 1, 1)", (us,))

    # moz_bookmarks link
    conn.execute("INSERT INTO moz_bookmarks VALUES (20, 1, 1, 10, 0, 'Rust Home', NULL, NULL, ?, 0, 'bm1')", (us,))
    conn.commit()
    conn.close()

    state = {}
    rows = list(
        sync_browser_pages(
            browser="firefox",
            profile_path=tmp_path,
            resource_state=state,
        )
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["url"] == "https://www.rust-lang.org/"
    assert row["visit_count"] == 10
    assert row["is_bookmarked"] is True
    assert row["bookmark_folder"] == "Bookmarks Toolbar/Rust"
    assert "browser:firefox" in row[NODE_SET_COLUMN]
    assert "browser:www.rust-lang.org" in row[NODE_SET_COLUMN]
    assert "browser:bookmarks/Bookmarks Toolbar/Rust" in row[NODE_SET_COLUMN]


# ---------------------------------------------------------------------------
# Noise & Redirect dropping
# ---------------------------------------------------------------------------


def test_drop_redirect_hops_chromium(tmp_path):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt = datetime(2026, 10, 1, tzinfo=timezone.utc)
    us = datetime_to_chromium_us(dt)

    conn = sqlite3.connect(history_file)
    # URL 1: Real visited page (transition 0 = LINK)
    conn.execute("INSERT INTO urls VALUES (1, 'https://example.com/target', 'Target', 1, 0, ?, 0)", (us,))
    conn.execute("INSERT INTO visits VALUES (101, 1, ?, 0, 0, 0)", (us,))

    # URL 2: Redirect hop (transition with CLIENT_REDIRECT 0x40000000 or hidden=1)
    conn.execute("INSERT INTO urls VALUES (2, 'https://example.com/redirect', 'Redirect', 1, 0, ?, 1)", (us,))
    conn.execute("INSERT INTO visits VALUES (102, 2, ?, 0, 0x40000000, 0)", (us,))
    conn.commit()
    conn.close()

    rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state={},
            drop_redirects=True,
        )
    )

    assert len(rows) == 1
    assert rows[0]["url"] == "https://example.com/target"


# ---------------------------------------------------------------------------
# Locked database & Safe copying tests
# ---------------------------------------------------------------------------


def test_copy_browser_db_success(tmp_path):
    db_file = tmp_path / "History"
    make_chromium_db(db_file)

    # Companion WAL and SHM
    wal_file = tmp_path / "History-wal"
    wal_file.write_text("wal data")

    with copy_browser_db(db_file) as copied:
        assert copied.exists()
        assert copied != db_file
        # Check SQLite connection works on copy
        conn = sqlite3.connect(copied)
        cur = conn.execute("SELECT count(*) FROM urls")
        assert cur.fetchone()[0] == 0
        conn.close()


def test_corrupted_copy_aborts_without_deleting(tmp_path):
    # If the database file is corrupted/unreadable, copy_browser_db raises RuntimeError
    bad_db = tmp_path / "History"
    bad_db.write_text("not a valid sqlite database header")

    with pytest.raises(RuntimeError):
        list(
            sync_browser_pages(
                browser="chrome",
                profile_path=tmp_path,
                resource_state={"known_ids": ["abc", "def"]},
            )
        )


# ---------------------------------------------------------------------------
# dlt Resource wiring test
# ---------------------------------------------------------------------------


def test_browser_source_dlt_wiring(tmp_path):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    resource = browser_source(profile_path=tmp_path, browser="chrome")

    # Document mode attribute
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == BROWSER_SOURCE_NAME
    assert getattr(resource, PIPELINE_SCOPE_ATTR).startswith("browser:chrome:")

    # Table name and primary key
    assert resource.name == BROWSER_TABLE_NAME
    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


# ---------------------------------------------------------------------------
# Filter tests: bookmarks_only, since, min_visits, include_domains
# ---------------------------------------------------------------------------


def test_bookmarks_only_filtering(tmp_path):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt = datetime(2026, 10, 1, tzinfo=timezone.utc)
    us = datetime_to_chromium_us(dt)

    conn = sqlite3.connect(history_file)
    conn.execute("INSERT INTO urls VALUES (1, 'https://docs.python.org/3/', 'Python Docs', 5, 0, ?, 0)", (us,))
    conn.execute("INSERT INTO visits VALUES (101, 1, ?, 0, 0, 0)", (us,))
    conn.execute("INSERT INTO urls VALUES (2, 'https://example.com/unbookmarked', 'Unbookmarked', 5, 0, ?, 0)", (us,))
    conn.execute("INSERT INTO visits VALUES (102, 2, ?, 0, 0, 0)", (us,))
    conn.commit()
    conn.close()

    bookmarks_file = tmp_path / "Bookmarks"
    bookmarks_file.write_text(
        json.dumps(
            {
                "roots": {
                    "bookmark_bar": {
                        "name": "Bookmarks",
                        "type": "folder",
                        "children": [
                            {
                                "name": "Python Docs",
                                "type": "url",
                                "url": "https://docs.python.org/3/",
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # When bookmarks_only=True, only the bookmarked page is emitted
    rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state={},
            bookmarks_only=True,
        )
    )
    assert len(rows) == 1
    assert rows[0]["url"] == "https://docs.python.org/3"


def test_since_filtering(tmp_path):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt_old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    dt_new = datetime(2026, 10, 5, tzinfo=timezone.utc)
    us_old = datetime_to_chromium_us(dt_old)
    us_new = datetime_to_chromium_us(dt_new)

    conn = sqlite3.connect(history_file)
    conn.execute("INSERT INTO urls VALUES (1, 'https://docs.python.org/old', 'Old', 1, 0, ?, 0)", (us_old,))
    conn.execute("INSERT INTO visits VALUES (101, 1, ?, 0, 0, 0)", (us_old,))
    conn.execute("INSERT INTO urls VALUES (2, 'https://docs.python.org/new', 'New', 1, 0, ?, 0)", (us_new,))
    conn.execute("INSERT INTO visits VALUES (102, 2, ?, 0, 0, 0)", (us_new,))
    conn.commit()
    conn.close()

    # Filter with since="2026-10-01"
    rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state={},
            since="2026-10-01T00:00:00Z",
        )
    )
    assert len(rows) == 1
    assert rows[0]["url"] == "https://docs.python.org/new"


def test_min_visits_and_include_domains_filtering(tmp_path):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt = datetime(2026, 10, 1, tzinfo=timezone.utc)
    us = datetime_to_chromium_us(dt)

    conn = sqlite3.connect(history_file)
    # 1 visit on python.org
    conn.execute("INSERT INTO urls VALUES (1, 'https://docs.python.org/low', 'Low', 1, 0, ?, 0)", (us,))
    # 5 visits on python.org
    conn.execute("INSERT INTO urls VALUES (2, 'https://docs.python.org/high', 'High', 5, 0, ?, 0)", (us,))
    # 5 visits on rust-lang.org
    conn.execute("INSERT INTO urls VALUES (3, 'https://www.rust-lang.org/', 'Rust', 5, 0, ?, 0)", (us,))
    conn.commit()
    conn.close()

    # min_visits=3 and include_domains=["python.org"]
    rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state={},
            min_visits=3,
            include_domains=["python.org"],
        )
    )
    assert len(rows) == 1
    assert rows[0]["url"] == "https://docs.python.org/high"


def test_fetch_content_mock(tmp_path, monkeypatch):
    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt = datetime(2026, 10, 1, tzinfo=timezone.utc)
    us = datetime_to_chromium_us(dt)

    conn = sqlite3.connect(history_file)
    conn.execute("INSERT INTO urls VALUES (1, 'https://example.com/article', 'Article', 1, 0, ?, 0)", (us,))
    conn.commit()
    conn.close()

    # Mock fetch_page_text
    def fake_fetch(url, timeout=10):
        return "This is the full fetched article body about PostgreSQL vacuuming."

    monkeypatch.setattr(
        "cognee_community_connector_browser.browser.fetch_page_text",
        fake_fetch,
    )

    rows = list(
        sync_browser_pages(
            browser="chrome",
            profile_path=tmp_path,
            resource_state={},
            fetch_content=True,
        )
    )
    assert len(rows) == 1
    content = rows[0]["content"]
    assert "This is the full fetched article body about PostgreSQL vacuuming." in content


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker
# ---------------------------------------------------------------------------


def test_forget_on_delete_end_to_end_through_real_dlt_merge(tmp_path):
    import dlt

    history_file = tmp_path / "History"
    make_chromium_db(history_file)

    dt1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    dt2 = datetime(2026, 10, 2, tzinfo=timezone.utc)
    us1 = datetime_to_chromium_us(dt1)
    us2 = datetime_to_chromium_us(dt2)

    conn = sqlite3.connect(history_file)
    conn.execute("INSERT INTO urls VALUES (1, 'https://docs.python.org/3/', 'Python Docs', 2, 0, ?, 0)", (us1,))
    conn.execute("INSERT INTO visits VALUES (101, 1, ?, 0, 0, 0)", (us1,))
    conn.execute("INSERT INTO urls VALUES (2, 'https://crates.io/', 'Crates', 1, 0, ?, 0)", (us2,))
    conn.execute("INSERT INTO visits VALUES (102, 2, ?, 0, 0, 0)", (us2,))
    conn.commit()
    conn.close()

    pipeline = dlt.pipeline(
        pipeline_name="test_browser_pipeline",
        destination=dlt.destinations.duckdb(str(tmp_path / "browser.duckdb")),
        dataset_name="browser_data",
        pipelines_dir=str(tmp_path / "dlt_pipelines"),
    )

    # First load: both rows land in duckdb
    src1 = browser_source(profile_path=tmp_path, browser="chrome")
    info1 = pipeline.run(src1)
    assert info1.has_failed_jobs is False

    with pipeline.sql_client() as client:
        with client.execute_query("SELECT id, url, _deleted FROM browser_pages ORDER BY id") as cur:
            res1 = cur.fetchall()
            assert len(res1) == 2
            assert all(r[2] is False for r in res1)

    # Second load: delete row 2 (crates.io) from browser history
    conn = sqlite3.connect(history_file)
    conn.execute("DELETE FROM urls WHERE id = 2")
    conn.execute("DELETE FROM visits WHERE url = 2")
    conn.commit()
    conn.close()

    src2 = browser_source(profile_path=tmp_path, browser="chrome")
    info2 = pipeline.run(src2)
    assert info2.has_failed_jobs is False

    # crates.io was hard-deleted by dlt's merge; only Python Docs remains!
    with pipeline.sql_client() as client:
        with client.execute_query("SELECT id, url FROM browser_pages") as cur:
            res2 = cur.fetchall()
            assert len(res2) == 1
            assert "docs.python.org" in res2[0][1]


