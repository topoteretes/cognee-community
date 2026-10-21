"""Notebooks: what a sync exports, what it skips, and what it forgets."""

import pytest
from fake_databricks import HOME, FakeWorkspace, source

from cognee_community_connector_databricks import (
    DatabricksAuthError,
    DatabricksConnectorError,
)
from cognee_community_connector_databricks.databricks import (
    _cells,
    _display_path,
    _render_notebook,
    _scrub,
    _sync_notebooks,
)

ROOT = f"{HOME}/analytics"
OTHER_ROOT = "/Shared/reports"


@pytest.fixture
def workspace():
    fake = FakeWorkspace()
    fake.notebook(f"{ROOT}/build_revenue", source("revenue = orders.groupBy('name')"))
    fake.notebook(f"{ROOT}/etl/load_orders", source("orders = spark.table('orders')"))
    fake.notebook(f"{ROOT}/report", source("SELECT 1", comment="--"), language="SQL")
    return fake


def sync(workspace, state, roots=(ROOT,), **kwargs):
    stats, failed = {}, set()
    workspace.calls.clear()
    rows = list(_sync_notebooks(workspace, list(roots), state, stats, failed, **kwargs))
    return rows, stats, failed


def titles(rows):
    return sorted(row["title"] for row in rows if not row["_deleted"])


def forgotten(rows):
    return sorted(row["id"] for row in rows if row["_deleted"])


def doc_id(workspace, path):
    return f"databricks:notebook:{workspace.objects[path]['object_id']}"


# -- first sync and change detection ---------------------------------------------


def test_first_sync_exports_every_notebook_in_the_tree(workspace):
    rows, stats, failed = sync(workspace, {})

    assert titles(rows) == ["build_revenue", "load_orders", "report"]
    assert stats == {"scanned": 3, "changed": 3}
    assert failed == set()


def test_rows_are_documents_with_a_stable_id(workspace):
    rows, _, _ = sync(workspace, {})

    row = next(r for r in rows if r["title"] == "build_revenue")
    assert row["id"] == doc_id(workspace, f"{ROOT}/build_revenue")
    assert row["content"].startswith("Databricks notebook build_revenue\nFolder: ~/analytics")
    assert row["_deleted"] is False


def test_only_notebooks_are_read_and_hidden_folders_are_skipped(workspace):
    workspace.other(f"{ROOT}/config.yaml", "FILE")
    workspace.other(f"{ROOT}/libs", "LIBRARY")
    workspace.notebook(f"{ROOT}/.internal/scratch", source("x = 1"))
    workspace.folder(f"{ROOT}/empty")

    rows, _, _ = sync(workspace, {})

    assert titles(rows) == ["build_revenue", "load_orders", "report"]
    assert f"{ROOT}/.internal" not in [p["path"] for _, p in workspace.calls]


def test_git_folders_are_walked_like_folders(workspace):
    workspace.folder(f"{ROOT}/repo", kind="REPO")
    workspace.notebook(f"{ROOT}/repo/train", source("model.fit()"))

    rows, _, _ = sync(workspace, {})

    assert "train" in titles(rows)


def test_unchanged_notebooks_are_not_exported_again(workspace):
    state = {}
    sync(workspace, state)

    rows, stats, _ = sync(workspace, state)

    assert rows == []
    assert workspace.exports() == []
    assert stats == {"scanned": 3}


def test_an_edited_notebook_is_the_only_one_exported(workspace):
    state = {}
    sync(workspace, state)
    workspace.edit(f"{ROOT}/report", source("SELECT 2", comment="--"))

    rows, stats, _ = sync(workspace, state)

    assert titles(rows) == ["report"]
    assert workspace.exports() == [f"{ROOT}/report"]
    assert stats["changed"] == 1


def test_a_new_notebook_is_exported(workspace):
    state = {}
    sync(workspace, state)
    workspace.notebook(f"{ROOT}/etl/load_customers", source("customers = 1"))

    rows, _, _ = sync(workspace, state)

    assert titles(rows) == ["load_customers"]


def test_full_refresh_exports_everything_again(workspace):
    state = {}
    sync(workspace, state)

    rows, _, _ = sync(workspace, state, full_refresh=True)

    assert titles(rows) == ["build_revenue", "load_orders", "report"]


# -- moves, renames and deletes -------------------------------------------------


def test_a_moved_or_renamed_notebook_keeps_its_document(workspace):
    state = {}
    sync(workspace, state)
    old_id = doc_id(workspace, f"{ROOT}/report")
    workspace.move(f"{ROOT}/report", f"{ROOT}/etl/daily_report")

    rows, _, _ = sync(workspace, state)

    # Same object, same document: nothing forgotten, nothing re-exported.
    assert forgotten(rows) == []
    assert workspace.exports() == []
    assert old_id == doc_id(workspace, f"{ROOT}/etl/daily_report")
    saved = state["notebooks"][old_id.rsplit(":", 1)[1]]
    assert saved["path"] == f"{ROOT}/etl/daily_report"


@pytest.mark.parametrize("folder", [False, True])
def test_a_deleted_notebook_or_folder_is_forgotten_once(workspace, folder):
    state = {}
    sync(workspace, state)
    gone = doc_id(workspace, f"{ROOT}/etl/load_orders")
    workspace.delete(f"{ROOT}/etl" if folder else f"{ROOT}/etl/load_orders")

    rows, stats, _ = sync(workspace, state)
    again, _, _ = sync(workspace, state)

    assert forgotten(rows) == [gone]
    assert stats["deleted"] == 1
    assert forgotten(again) == []


def test_a_notebook_replaced_at_the_same_path_forgets_the_old_one(workspace):
    state = {}
    sync(workspace, state)
    old = doc_id(workspace, f"{ROOT}/report")
    workspace.delete(f"{ROOT}/report")
    workspace.notebook(f"{ROOT}/report", source("SELECT 3", comment="--"), language="SQL")

    rows, _, _ = sync(workspace, state)

    assert forgotten(rows) == [old]
    assert titles(rows) == ["report"]


def test_a_notebook_moved_out_of_the_selected_folder_is_forgotten(workspace):
    state = {}
    sync(workspace, state)
    moved = doc_id(workspace, f"{ROOT}/report")
    workspace.move(f"{ROOT}/report", f"{HOME}/archive/report")

    rows, _, _ = sync(workspace, state)

    assert forgotten(rows) == [moved]


def test_a_notebook_the_listing_misses_but_that_is_still_there_is_kept(workspace):
    state = {}
    sync(workspace, state)
    original = workspace.get

    def listing_without_report(path, params=None):
        answer = original(path, params)
        if path.endswith("/list") and params["path"] == ROOT:
            answer = {
                "objects": [o for o in answer["objects"] if not o["path"].endswith("/report")]
            }
        return answer

    workspace.get = listing_without_report

    rows, _, _ = sync(workspace, state)

    # The lookup by path still finds the same notebook: one signal contradicts
    # the other, so nothing is forgotten.
    assert forgotten(rows) == []


def test_a_folder_removed_from_the_selection_forgets_its_notebooks(workspace):
    workspace.notebook(f"{OTHER_ROOT}/weekly", source("weekly = 1"))
    state = {}
    sync(workspace, state, roots=(ROOT, OTHER_ROOT))
    weekly = doc_id(workspace, f"{OTHER_ROOT}/weekly")

    rows, _, _ = sync(workspace, state, roots=(ROOT,))

    assert forgotten(rows) == [weekly]


def test_a_notebook_deleted_between_listing_and_export_is_forgotten_next_time(workspace):
    state = {}
    sync(workspace, state)
    workspace.edit(f"{ROOT}/report", source("SELECT 9", comment="--"))
    gone = doc_id(workspace, f"{ROOT}/report")
    original = workspace.get

    def delete_on_export(path, params=None):
        if path.endswith("/export"):
            workspace.delete(params["path"])
        return original(path, params)

    workspace.get = delete_on_export
    rows, _, _ = sync(workspace, state)
    assert rows == []

    workspace.get = original
    rows, _, _ = sync(workspace, state)
    assert forgotten(rows) == [gone]


# -- failures never delete ------------------------------------------------------


def test_a_root_that_fails_to_list_keeps_its_state_and_forgets_nothing(workspace):
    workspace.notebook(f"{OTHER_ROOT}/weekly", source("weekly = 1"))
    state = {}
    sync(workspace, state, roots=(ROOT, OTHER_ROOT))
    before = dict(state["notebooks"])
    workspace.delete(f"{OTHER_ROOT}/weekly")
    workspace.edit(f"{ROOT}/report", source("SELECT 2", comment="--"))
    workspace.failing[OTHER_ROOT] = DatabricksConnectorError("Databricks returned HTTP 500.")

    rows, _, failed = sync(workspace, state, roots=(ROOT, OTHER_ROOT))

    assert failed == {OTHER_ROOT}
    assert forgotten(rows) == []
    assert titles(rows) == ["report"]
    weekly = doc_id_from(before, OTHER_ROOT)
    assert state["notebooks"][weekly] == before[weekly]


def doc_id_from(saved, root):
    return next(i for i, n in saved.items() if n["root"] == root)


def test_a_subfolder_that_fails_fails_its_whole_root(workspace):
    state = {}
    sync(workspace, state)
    workspace.delete(f"{ROOT}/report")
    workspace.failing[f"{ROOT}/etl"] = DatabricksConnectorError("Databricks returned HTTP 503.")

    rows, _, failed = sync(workspace, state)

    # A partial walk would make everything under etl look deleted.
    assert failed == {ROOT}
    assert rows == []


def test_an_error_during_the_lookup_forgets_nothing(workspace):
    state = {}
    sync(workspace, state)
    workspace.delete(f"{ROOT}/report")
    workspace.failing["/get-status"] = DatabricksConnectorError("Databricks returned HTTP 500.")

    rows, _, failed = sync(workspace, state)

    assert forgotten(rows) == []
    assert failed == {ROOT}


def test_a_refused_token_stops_the_whole_sync(workspace):
    workspace.failing[ROOT] = DatabricksAuthError("Databricks refused the token (HTTP 403).")

    with pytest.raises(DatabricksAuthError):
        sync(workspace, {})


def test_a_notebook_under_two_selected_folders_is_synced_once(workspace):
    rows, stats, _ = sync(workspace, {}, roots=(ROOT, f"{ROOT}/etl"))

    assert titles(rows) == ["build_revenue", "load_orders", "report"]
    assert stats["scanned"] == 3


def test_logs_carry_counts_only(workspace, monkeypatch):
    import cognee_community_connector_databricks.databricks as module

    messages = []
    monkeypatch.setattr(
        module.logger, "warning", lambda message, *args: messages.append(message % args)
    )
    workspace.failing[ROOT] = DatabricksConnectorError("Databricks returned HTTP 500.")

    sync(workspace, {})

    assert messages
    for private in ("someone@example.com", "analytics", "build_revenue"):
        assert all(private not in message for message in messages)


# -- rendering --------------------------------------------------------------------


def entry(path=f"{ROOT}/build_revenue", language="PYTHON"):
    return {"path": path, "language": language}


def test_a_notebook_reads_as_prose_and_code():
    text, _ = _render_notebook(
        entry(),
        source(
            "# MAGIC %md\n# MAGIC # Build revenue\n# MAGIC Joins orders to customers.",
            "# DBTITLE 1,Load the tables\norders = spark.table('workspace.sales.orders')",
            "# MAGIC %sql\n# MAGIC SELECT * FROM workspace.sales.orders",
            "# MAGIC %run ./helpers",
        ),
    )

    assert text == (
        "Databricks notebook build_revenue\n"
        "Folder: ~/analytics\n"
        "Language: Python\n\n"
        "# Build revenue\nJoins orders to customers.\n\n"
        "Load the tables\n\n"
        "```python\norders = spark.table('workspace.sales.orders')\n```\n\n"
        "```sql\nSELECT * FROM workspace.sales.orders\n```\n\n"
        "```text\n%run ./helpers\n```"
    )


@pytest.mark.parametrize(
    ("language", "comment", "label"),
    [("SQL", "--", "sql"), ("SCALA", "//", "scala"), ("R", "#", "r")],
)
def test_every_notebook_language_is_split_into_cells(language, comment, label):
    text, _ = _render_notebook(
        entry(language=language),
        source(f"{comment} MAGIC %md\n{comment} MAGIC Notes", "code here", comment=comment),
    )

    assert text.endswith(f"Notes\n\n```{label}\ncode here\n```")


def test_empty_cells_are_left_out():
    assert _cells(source("x = 1", "", "   ", "# MAGIC %md\n# MAGIC"), "python") == [
        ("python", "x = 1")
    ]


def test_a_file_without_the_notebook_header_is_one_code_cell():
    assert _cells("print('hi')\n", "python") == [("python", "print('hi')")]


def test_the_same_notebook_renders_to_the_same_text():
    notebook = source("# MAGIC %md\n# MAGIC Title", "x = 1")

    assert _render_notebook(entry(), notebook) == _render_notebook(entry(), notebook)


@pytest.mark.parametrize(
    ("path", "shown"),
    [
        ("/Users/someone@example.com/analytics", "~/analytics"),
        ("/Users/someone@example.com", "~"),
        ("/Repos/someone@example.com/project", "/Repos/~/project"),
        ("/Shared/reports", "/Shared/reports"),
        ("/Users/4b44bb25-service-principal/jobs", "/Users/4b44bb25-service-principal/jobs"),
    ],
)
def test_folders_never_show_a_users_email(path, shown):
    assert _display_path(path) == shown


# -- secret scrubbing ------------------------------------------------------------


# The fake secrets are built from pieces, so no secret scanner takes this file
# for a leak.
@pytest.mark.parametrize(
    "secret",
    [
        'password = "hunter2hunter2"',
        "API_KEY = 'sk-live-1234567890abcdef'",
        '"client_secret": "abcd1234efgh"',
        'spark.conf.set("fs.azure.account.key.store.dfs.core.windows.net", "c2VjcmV0a2V5")',
        "token = '" + "dapi" + "0123456789abcdef" * 2 + "'",
        "aws_access_key_id = " + "AKIA" + "ABCDEFGHIJKLMNOP",
        "headers = {'Authorization': 'Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig'}",
        "url = 'postgresql://admin:S3cretPass@db.example.com:5432/sales'",
        "gh = 'ghp_" + "a" * 36 + "'",
        "slack = '" + "xoxb" + "-1234567890-abcdefghij'",
        "maps = 'AIza" + "B" * 35 + "'",
        "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA " + "PRIVATE KEY-----",
    ],
)
def test_a_hardcoded_secret_is_scrubbed(secret):
    text, count = _scrub(f"before\n{secret}\nafter")

    assert count == 1
    assert "[REDACTED]" in text
    for piece in (
        "hunter2",
        "sk-live",
        "abcd1234",
        "c2VjcmV0",
        "dapi0",
        "AKIAABC",
        "eyJhbG",
        "S3cret",
        "ghp_",
        "xoxb-",
        "AIzaB",
        "MIIEow",
    ):
        assert piece not in text
    assert text.startswith("before\n") and text.endswith("\nafter")


@pytest.mark.parametrize(
    "harmless",
    [
        'token = dbutils.secrets.get(scope="kv", key="token")',
        "password = os.environ['DB_PASSWORD']",
        "df = spark.table('workspace.sales.orders')",
        'password = ""',
        "url = 'https://example.com/path'",
    ],
)
def test_code_that_holds_no_secret_is_left_alone(harmless):
    assert _scrub(harmless) == (harmless, 0)


def test_a_secret_matched_twice_is_counted_once():
    text, count = _scrub("API_KEY = 'sk-live-1234567890abcdef'")

    assert (text, count) == ("API_KEY = '[REDACTED]'", 1)


def test_planted_secrets_never_reach_the_document(workspace):
    workspace.notebook(
        f"{ROOT}/leaky",
        source(
            "# MAGIC %md\n# MAGIC Connects with password hunter2hunter2",
            'password = "hunter2hunter2"\nkey = "sk-live-1234567890abcdef"',
        ),
    )

    rows, stats, _ = sync(workspace, {})

    leaky = next(row for row in rows if row["title"] == "leaky")
    assert 'password = "[REDACTED]"' in leaky["content"]
    assert "sk-live" not in leaky["content"]
    assert stats["redacted"] == 2
