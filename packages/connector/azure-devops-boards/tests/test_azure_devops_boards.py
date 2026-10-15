"""Unit tests for the Azure DevOps Boards connector.

The Azure DevOps REST API is faked by ``FakeAzureDevOps``, so these need no
account or network. They cover the revisions cursor, batching, comments,
rendering, the three ways an item leaves memory (recycle bin, destroyed, moved
out of scope), the safety guard on an empty sweep, the HTTP client's
Retry-After handling, the dlt resource wiring, and a real dlt merge acting on
the tombstones.
"""

from __future__ import annotations

import pytest

from cognee_community_connector_azure_devops_boards import azure_devops_boards as boards
from cognee_community_connector_azure_devops_boards.azure_devops_boards import (
    AzureDevOpsClient,
    _clean_html,
    _Scope,
    azure_devops_boards_source,
    render_work_item,
    sync_work_items,
)

ORG_URL = "https://dev.azure.com/acme"
PROJECT = "Shop"


# ---------------------------------------------------------------------------
# Fake Azure DevOps
# ---------------------------------------------------------------------------
def work_item(wid, title="", *, type_="Bug", area="Shop", **extra_fields):
    fields = {
        "System.Id": wid,
        "System.Title": title or f"Item {wid}",
        "System.WorkItemType": type_,
        "System.State": "Active",
        "System.AreaPath": area,
        **extra_fields,
    }
    return {"id": wid, "fields": fields, "relations": []}


class FakeAzureDevOps:
    """In-memory stand-in for the endpoints the connector calls.

    Every change bumps a global revision number, and the revisions feed's
    continuation token is simply the last revision number a caller has seen.
    """

    def __init__(self, items=()):
        self.organization_url = ORG_URL
        self.items: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.recycle_bin: set[int] = set()
        self.changed_at: dict[int, int] = {}
        self.revision = 0
        self.calls: list[tuple[str, str]] = []
        self.batch_sizes: list[int] = []
        self.fail_teams = False
        self.fail_batch = False
        self.sweep_returns_nothing = False
        for item in items:
            self.upsert(item)

    # -- test helpers -------------------------------------------------------
    def _touch(self, wid):
        self.revision += 1
        self.changed_at[wid] = self.revision

    def upsert(self, item):
        self.items[item["id"]] = item
        self.recycle_bin.discard(item["id"])
        self._touch(item["id"])

    def add_comment(self, wid, text, author="Ravi Kumar", date="2026-10-02T10:00:00Z", **extra):
        self.comments.setdefault(wid, []).append(
            {"text": text, "createdBy": {"displayName": author}, "createdDate": date, **extra}
        )
        fields = self.items[wid]["fields"]
        fields["System.CommentCount"] = fields.get("System.CommentCount", 0) + 1
        self._touch(wid)

    def delete(self, wid):
        self.recycle_bin.add(wid)
        self._touch(wid)

    def destroy(self, wid):
        # Destroyed items leave no revision behind.
        self.items.pop(wid, None)
        self.recycle_bin.discard(wid)
        self.changed_at.pop(wid, None)

    # -- client interface ---------------------------------------------------
    def get(self, path, params=None):
        params = params or {}
        self.calls.append(("GET", path))
        if path.endswith("/_apis/wit/reporting/workitemrevisions"):
            since = int(params.get("continuationToken") or 0)
            values = [
                {"id": wid, "rev": rev, "fields": {"System.Id": wid}}
                for wid, rev in sorted(self.changed_at.items(), key=lambda kv: kv[1])
                if rev > since
            ]
            return {"values": values, "continuationToken": str(self.revision), "isLastBatch": True}
        if "/comments" in path:
            wid = int(path.split("/workItems/")[1].split("/")[0])
            return {"comments": list(self.comments.get(wid, []))}
        if path.endswith("/teams"):
            if self.fail_teams:
                raise RuntimeError("403 Forbidden")
            return {"value": [{"name": "Payments team"}]}
        if path.endswith("/_apis/work/boards"):
            return {"value": [{"id": "b1", "name": "Stories"}]}
        if path.endswith("/_apis/work/boards/b1"):
            return {"fields": {"columnField": {"referenceName": "WEF_ABC123_Kanban.Column"}}}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body, params=None):
        params = params or {}
        self.calls.append(("POST", path))
        if path.endswith("/_apis/wit/workitemsbatch"):
            if self.fail_batch:
                raise RuntimeError("500 Server Error")
            self.batch_sizes.append(len(body["ids"]))
            assert len(body["ids"]) <= boards.BATCH_SIZE
            assert body["errorPolicy"] == "Omit"
            return {
                "value": [
                    self.items[i] if i in self.items and i not in self.recycle_bin else None
                    for i in body["ids"]
                ]
            }
        if path.endswith("/_apis/wit/wiql"):
            if self.sweep_returns_nothing:
                return {"workItems": []}
            last_id = int(body["query"].split("[System.Id] >")[1].split()[0])
            top = int(params["$top"])
            live = sorted(i for i in self.items if i not in self.recycle_bin and i > last_id)
            return {"workItems": [{"id": i} for i in live[:top]]}
        raise AssertionError(f"unexpected POST {path}")


def run(fake, state, **scope_kwargs):
    scope = _Scope(project=PROJECT, **scope_kwargs)
    return list(sync_work_items(fake, scope, state))


def ids(rows, deleted=False):
    return sorted(int(r["id"]) for r in rows if r["_deleted"] is deleted)


# ---------------------------------------------------------------------------
# Sync behaviour
# ---------------------------------------------------------------------------
def test_first_sync_yields_every_work_item_and_records_cursor():
    fake = FakeAzureDevOps([work_item(1, "Login fails"), work_item(2, "Add dark mode")])
    state: dict = {}

    rows = run(fake, state)

    assert ids(rows) == [1, 2]
    assert state["continuation_token"] == str(fake.revision)
    assert state["known_ids"] == [1, 2]
    first = next(r for r in rows if r["id"] == "1")
    assert first["title"] == "Bug #1: Login fails"
    assert first["url"] == f"{ORG_URL}/Shop/_workitems/edit/1"


def test_second_sync_with_no_changes_yields_nothing():
    fake = FakeAzureDevOps([work_item(1), work_item(2)])
    state: dict = {}
    run(fake, state)

    assert run(fake, state) == []
    assert state["known_ids"] == [1, 2]


def test_only_changed_items_are_fetched_again():
    fake = FakeAzureDevOps([work_item(1), work_item(2), work_item(3)])
    state: dict = {}
    run(fake, state)

    fake.upsert(work_item(2, "Renamed"))
    rows = run(fake, state)

    assert ids(rows) == [2]
    assert rows[0]["title"] == "Bug #2: Renamed"


def test_new_comment_re_syncs_the_work_item_with_the_comment():
    fake = FakeAzureDevOps([work_item(1)])
    state: dict = {}
    run(fake, state)

    fake.add_comment(1, "<div>Only happens when <b>3DS</b> is triggered.</div>")
    rows = run(fake, state)

    assert ids(rows) == [1]
    assert "Ravi Kumar on 2026-10-02: Only happens when 3DS is triggered." in rows[0]["content"]


def test_work_item_moved_to_recycle_bin_becomes_a_tombstone():
    fake = FakeAzureDevOps([work_item(1), work_item(2)])
    state: dict = {}
    run(fake, state)

    fake.delete(2)
    rows = run(fake, state)

    assert ids(rows, deleted=True) == [2]
    assert ids(rows) == []
    assert state["known_ids"] == [1]


def test_destroyed_work_item_is_caught_by_the_id_sweep():
    fake = FakeAzureDevOps([work_item(1), work_item(2)])
    state: dict = {}
    run(fake, state)

    fake.destroy(2)  # leaves no revision, so only the sweep can see it
    rows = run(fake, state)

    assert rows == [{"id": "2", "_deleted": True}]
    assert state["known_ids"] == [1]


def test_empty_sweep_deletes_nothing_and_keeps_known_ids():
    fake = FakeAzureDevOps([work_item(1), work_item(2)])
    state: dict = {}
    run(fake, state)

    fake.sweep_returns_nothing = True
    rows = run(fake, state)

    assert rows == []
    assert state["known_ids"] == [1, 2]


def test_restored_work_item_comes_back():
    fake = FakeAzureDevOps([work_item(1)])
    state: dict = {}
    run(fake, state)
    fake.delete(1)
    run(fake, state)

    fake.upsert(work_item(1, "Restored"))
    rows = run(fake, state)

    assert ids(rows) == [1]
    assert state["known_ids"] == [1]


def test_work_item_moved_out_of_area_path_is_forgotten():
    fake = FakeAzureDevOps(
        [work_item(1, area="Shop\\Payments"), work_item(2, area="Shop\\Payments\\Cards")]
    )
    state: dict = {}
    assert ids(run(fake, state, area_paths=["Shop\\Payments"])) == [1, 2]

    fake.upsert(work_item(2, area="Shop\\Search"))
    rows = run(fake, state, area_paths=["Shop\\Payments"])

    assert ids(rows, deleted=True) == [2]


def test_area_path_match_does_not_leak_into_sibling_paths():
    fake = FakeAzureDevOps([work_item(1, area="Shop\\Pay"), work_item(2, area="Shop\\Payments")])

    rows = run(fake, {}, area_paths=["Shop\\Pay"])

    assert ids(rows) == [1]


def test_work_item_whose_type_changes_out_of_the_filter_is_forgotten():
    fake = FakeAzureDevOps([work_item(1, type_="Bug"), work_item(2, type_="Task")])
    state: dict = {}
    assert ids(run(fake, state, work_item_types=["Bug"])) == [1]

    fake.upsert(work_item(1, type_="Task"))
    rows = run(fake, state, work_item_types=["Bug"])

    assert ids(rows, deleted=True) == [1]


def test_changed_items_are_fetched_in_batches_of_200():
    fake = FakeAzureDevOps([work_item(i) for i in range(1, 451)])

    rows = run(fake, {}, include_comments=False)

    assert len(rows) == 450
    assert fake.batch_sizes == [200, 200, 50]


def test_id_sweep_pages_by_id_range(monkeypatch):
    monkeypatch.setattr(boards, "SWEEP_PAGE_SIZE", 2)
    fake = FakeAzureDevOps([work_item(i) for i in range(1, 6)])
    state: dict = {}
    run(fake, state, include_comments=False)

    fake.destroy(5)
    rows = run(fake, state, include_comments=False)

    wiql_calls = [c for c in fake.calls if c[1].endswith("/wiql")]
    assert len(wiql_calls) >= 3
    assert rows == [{"id": "5", "_deleted": True}]


def test_failed_fetch_raises_and_leaves_the_cursor_alone():
    fake = FakeAzureDevOps([work_item(1)])
    state: dict = {}
    run(fake, state)
    saved = dict(state)

    fake.upsert(work_item(1, "Changed"))
    fake.fail_batch = True
    with pytest.raises(RuntimeError):
        run(fake, state)

    assert state == saved


def test_comments_are_not_fetched_when_the_item_has_none():
    fake = FakeAzureDevOps([work_item(1, **{"System.CommentCount": 0})])

    run(fake, {})

    assert not any("/comments" in path for _, path in fake.calls)


def test_batches_are_fetched_lazily_as_rows_are_consumed():
    fake = FakeAzureDevOps([work_item(i) for i in range(1, 451)])
    rows = sync_work_items(fake, _Scope(project=PROJECT, include_comments=False), {})

    next(rows)

    assert fake.batch_sizes == [200]


def test_comments_can_be_switched_off():
    fake = FakeAzureDevOps([work_item(1)])
    fake.add_comment(1, "secret internal note")

    rows = run(fake, {}, include_comments=False)

    assert "secret internal note" not in rows[0]["content"]
    assert not any("/comments" in path for _, path in fake.calls)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_clean_html_keeps_line_breaks_and_drops_tags():
    raw = "<div>Steps:</div><ol><li>Open&nbsp;checkout</li><li>Pay</li></ol><br>Done"
    assert _clean_html(raw) == "Steps:\nOpen checkout\nPay\n\nDone"


def test_render_includes_fields_links_sections_and_comments_but_no_emails():
    item = work_item(
        412,
        "Card payments fail on Safari 17",
        area="Shop\\Payments",
        **{
            "System.AssignedTo": {"displayName": "Priya Nair", "uniqueName": "priya@acme.com"},
            "System.CreatedBy": "Ravi Kumar <ravi@acme.com>",
            "Microsoft.VSTS.Common.Priority": 1,
            "Microsoft.VSTS.TCM.ReproSteps": "<p>Pay with a saved card</p>",
            "System.Rev": 9,
            "System.ChangedDate": "2026-10-07T10:00:00Z",
        },
    )
    item["relations"] = [
        {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": f"{ORG_URL}/_apis/wit/workItems/398"},
        {"rel": "System.LinkTypes.Related", "url": f"{ORG_URL}/_apis/wit/workItems/7"},
        {"rel": "ArtifactLink", "url": "vstfs:///Git/PullRequestId/p1%2Fr1%2F87"},
        {"rel": "AttachedFile", "url": f"{ORG_URL}/_apis/wit/attachments/abc"},
    ]
    comments = [
        {"text": "Only with 3DS", "createdBy": {"displayName": "Ravi Kumar"}, "createdDate": ""},
        {"text": "removed", "createdBy": {"displayName": "X"}, "isDeleted": True},
    ]

    text = render_work_item(item, comments)

    assert "Assigned to: Priya Nair" in text
    assert "Created by: Ravi Kumar" in text
    assert "Parent: work item #398" in text
    assert "Related: work item #7" in text
    assert "Pull requests: pull request !87" in text
    assert "Repro steps:\nPay with a saved card" in text
    assert "- Ravi Kumar: Only with 3DS" in text
    assert "removed" not in text
    assert "@acme.com" not in text
    # Revision noise stays out so unchanged items keep the same content hash.
    assert "2026-10-07" not in text
    assert "Rev" not in text


def test_board_columns_are_labelled_with_team_and_board():
    fake = FakeAzureDevOps(
        [work_item(1, **{"WEF_ABC123_Kanban.Column": "Doing", "System.BoardColumn": "Doing"})]
    )

    rows = run(fake, {})

    assert "Board column: Doing (Payments team / Stories)" in rows[0]["content"]


def test_board_columns_fall_back_to_raw_values_without_team_access():
    fake = FakeAzureDevOps([work_item(1, **{"WEF_ABC123_Kanban.Column": "Doing"})])
    fake.fail_teams = True

    rows = run(fake, {})

    assert "Board column: Doing" in rows[0]["content"]
    assert "Payments team" not in rows[0]["content"]


def test_board_lookups_are_skipped_when_no_item_is_on_a_board():
    fake = FakeAzureDevOps([work_item(1)])

    run(fake, {})

    assert not any(path.endswith("/teams") for _, path in fake.calls)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers: dict = {}
        self.auth = None
        self.requests: list = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.fixture
def clock(monkeypatch):
    """Freeze time.monotonic and record sleeps instead of waiting."""
    now = {"t": 1000.0}
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(round(seconds, 3))
        now["t"] += seconds

    monkeypatch.setattr(boards.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(boards.time, "sleep", sleep)
    return sleeps


def test_client_sends_the_pat_as_basic_auth_with_empty_username():
    session = _Session([_Response(200, {"ok": True})])
    client = AzureDevOpsClient(ORG_URL + "/", "my-pat", session=session)

    assert client.get("Shop/_apis/x", {"a": 1}) == {"ok": True}
    assert session.auth == ("", "my-pat")
    method, url, kwargs = session.requests[0]
    assert (method, url) == ("GET", f"{ORG_URL}/Shop/_apis/x")
    assert kwargs["params"] == {"a": 1}


def test_client_retries_429_after_the_retry_after_delay(clock):
    session = _Session(
        [_Response(429, headers={"Retry-After": "3"}), _Response(200, {"value": [1]})]
    )
    client = AzureDevOpsClient(ORG_URL, "pat", session=session)

    assert client.get("x") == {"value": [1]}
    assert clock == [3.0]


def test_client_honours_retry_after_sent_on_a_200(clock):
    # Azure DevOps sends Retry-After on a successful response as an early
    # warning. The next request has to wait for it.
    session = _Session(
        [_Response(200, {"n": 1}, headers={"Retry-After": "2"}), _Response(200, {"n": 2})]
    )
    client = AzureDevOpsClient(ORG_URL, "pat", session=session)

    assert client.get("a") == {"n": 1}
    assert clock == []
    assert client.get("b") == {"n": 2}
    assert clock == [2.0]


def test_client_backs_off_on_server_errors_then_gives_up(clock):
    session = _Session([_Response(503) for _ in range(boards._MAX_RETRIES)])
    client = AzureDevOpsClient(ORG_URL, "pat", session=session)

    with pytest.raises(RuntimeError, match="503"):
        client.get("x")
    assert clock == [1.0, 2.0, 4.0, 8.0]


def test_client_does_not_retry_auth_errors(clock):
    session = _Session([_Response(401)])
    client = AzureDevOpsClient(ORG_URL, "pat", session=session)

    with pytest.raises(RuntimeError, match="401"):
        client.get("x")
    assert clock == []


# ---------------------------------------------------------------------------
# dlt wiring
# ---------------------------------------------------------------------------
def test_resource_is_a_merge_document_source_with_hard_delete():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, document_source_tag

    resource = azure_devops_boards_source(project=PROJECT, client=FakeAzureDevOps())

    assert resource.name == "azure_devops_acme_shop"
    assert document_source_tag(resource) == "azure_devops_boards"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "azure_devops_acme_shop"
    schema = resource.compute_table_schema()
    disposition = schema.get("write_disposition")
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_each_project_gets_its_own_resource_and_cursor():
    def name(org_url, project):
        client = FakeAzureDevOps()
        client.organization_url = org_url
        return azure_devops_boards_source(project=project, client=client).name

    assert name(ORG_URL, "Shop") != name(ORG_URL, "Warehouse")
    assert name(ORG_URL, "Shop") != name("https://dev.azure.com/other", "Shop")
    assert name("https://tfs.acme.local/DefaultCollection", "My Project") == (
        "azure_devops_defaultcollection_my_project"
    )


def test_source_needs_an_organization_and_a_token(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    with pytest.raises(ValueError, match="organization"):
        azure_devops_boards_source(project=PROJECT, pat="x")
    with pytest.raises(ValueError, match="AZURE_DEVOPS_PAT"):
        azure_devops_boards_source(organization="acme", project=PROJECT)


def test_source_reads_the_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "from-env")
    resource = azure_devops_boards_source(organization="acme", project=PROJECT)
    assert resource.name == "azure_devops_acme_shop"


def test_tombstones_remove_rows_in_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    fake = FakeAzureDevOps([work_item(1), work_item(2), work_item(3)])
    pipeline = dlt.pipeline(
        pipeline_name="test_azure_devops_boards_e2e",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.duckdb(str(tmp_path / "boards.duckdb")),
        dataset_name="backlog",
    )

    def table_ids():
        with pipeline.sql_client() as client:
            rows = client.execute_sql("SELECT id FROM azure_devops_acme_shop ORDER BY id")
        return [r[0] for r in rows]

    pipeline.run(azure_devops_boards_source(project=PROJECT, client=fake))
    assert table_ids() == ["1", "2", "3"]

    # One item to the recycle bin, one destroyed outright, one untouched. The
    # second run only sends tombstones, and merge keeps the untouched row.
    fake.delete(2)
    fake.destroy(3)
    pipeline.run(azure_devops_boards_source(project=PROJECT, client=fake))
    assert table_ids() == ["1"]
