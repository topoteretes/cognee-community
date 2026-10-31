import copy
import re
from datetime import UTC, datetime
from unittest.mock import patch

import dlt
import pytest

from cognee_community_connector_jira import jira_source
from cognee_community_connector_jira._verified import get_paginated_data
from cognee_community_connector_jira.attachments import extract_text
from cognee_community_connector_jira.render import adf_text
from cognee_community_connector_jira.snapshot import window
from cognee_community_connector_jira.transport import (
    JiraReadError,
    numeric_id,
    site_root,
)


def adf(text):
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class Reader:
    site_url = "https://synthetic.atlassian.net"

    def __init__(self):
        self.owner = "synthetic-owner"
        self.timezone = "Asia/Kolkata"
        self.calls = []
        self.changed = set()
        self.fail = None
        self.issues = {
            str(i): {
                "id": str(i),
                "key": f"KAN-{i}",
                "fields": {
                    "summary": f"Issue {i}",
                    "description": adf(f"Description {i}"),
                    "updated": "2026-10-21T07:00:00.000+0000",
                    "project": {"id": "100", "key": "KAN", "name": "Synthetic"},
                    "status": {"name": "To Do"},
                    "labels": ["synthetic"],
                },
            }
            for i in (1, 2)
        }
        self.comment_items = [
            {
                "id": "11",
                "body": adf("old comment"),
                "author": {"displayName": "Author"},
                "created": "2026-10-20",
            },
            {"id": "12", "body": adf("second comment")},
        ]
        self.files = [
            {
                "id": "21",
                "filename": "fixture.txt",
                "mimeType": "text/plain",
                "size": 5,
                "content": "https://evil.invalid/not-fetched",
            }
        ]
        self.blob = b"hello"

    def json(self, path, params=None):
        params = copy.deepcopy(params or {})
        self.calls.append((path, params))
        if self.fail and self.fail(path, params):
            raise JiraReadError("Synthetic failure")
        if path.endswith("myself"):
            return {"accountId": self.owner, "timeZone": self.timezone}
        if path.endswith("search/jql"):
            issues = list(self.issues.values())
            if '"OTHER"' in params["jql"]:
                issues = []
            if "updated >=" in params["jql"]:
                issues = [x for x in issues if x["id"] in self.changed]
            match = re.search(r"id in \(([^)]+)\)", params["jql"])
            if match:
                issues = [x for x in issues if x["id"] in match[1].split(",")]
            offset = int(params.get("nextPageToken", 0))
            page = copy.deepcopy(issues[offset : offset + params["maxResults"]])
            if params["fields"] != "*all":
                for item in page:
                    item["fields"].pop("description")
            next_offset = offset + len(page)
            return {
                "issues": page,
                "isLast": next_offset == len(issues),
                **({"nextPageToken": str(next_offset)} if next_offset < len(issues) else {}),
            }
        identity = path.split("/")[4]
        if path.endswith("/comment"):
            items = self.comment_items if identity == "1" else []
            offset = params["startAt"]
            return {
                "startAt": offset,
                "total": len(items),
                "comments": copy.deepcopy(items[offset : offset + params["maxResults"]]),
            }
        if params.get("fields") == "attachment":
            return {
                "id": identity,
                "fields": {"attachment": copy.deepcopy(self.files if identity == "1" else [])},
            }
        return copy.deepcopy(self.issues[identity])

    def attachment(self, identity, limit):
        self.calls.append(("attachment", {"id": identity, "limit": limit}))
        if len(self.blob) > limit:
            raise JiraReadError("too big")
        return self.blob


def source(client, **kwargs):
    return jira_source(
        client.site_url,
        "owner@example.test",
        "synthetic-token",
        ["KAN"],
        resource_name="test",
        missing_policy=kwargs.pop("missing_policy", "require_confirmation"),
        client=client,
        page_size=1,
        **kwargs,
    )


@pytest.fixture
def pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="jira_test",
        pipelines_dir=str(tmp_path / "pipelines"),
        dataset_name="jira_test",
        destination=dlt.destinations.sqlalchemy(
            credentials={"drivername": "sqlite", "database": str(tmp_path / "stage.db")}
        ),
    )


def loaded(pipeline, src):
    with pipeline.sql_client() as sql:
        return [
            tuple(row)
            for row in sql.execute_sql(f"SELECT id, title, content FROM {src.name} ORDER BY id")
        ]


def test_public_factory_lazy_and_scope():
    client = Reader()
    one = source(client)
    assert not client.calls
    two = jira_source(
        client.site_url,
        "owner@example.test",
        "rotated-token",
        ["KAN"],
        resource_name="test",
        missing_policy="require_confirmation",
        client=client,
    )
    assert one.name == two.name
    assert one.cognee_pipeline_scope == two.cognee_pipeline_scope
    assert (
        one.name
        != jira_source(
            client.site_url,
            "owner@example.test",
            "token",
            ["KAN"],
            resource_name="other",
            missing_policy="require_confirmation",
            client=client,
        ).name
    )
    assert (
        one.name
        == jira_source(
            client.site_url,
            "owner@example.test",
            "token",
            ["OTHER"],
            resource_name="test",
            missing_policy="require_confirmation",
            client=client,
        ).name
    )


def test_snapshot_incremental_children_and_final_empty(pipeline):
    client = Reader()
    src = source(client)
    pipeline.run(src)
    initial = loaded(pipeline, src)
    assert len(initial) == 5
    assert "hello" in str(initial)
    client.calls.clear()
    src = source(client)
    pipeline.run(src)
    assert loaded(pipeline, src) == initial
    assert src.cognee_sync_stats["reused"] == 2
    assert not any(
        p == "attachment" or (p.endswith("/1") and q.get("fields") == "*all")
        for p, q in client.calls
    )
    client.comment_items[0]["body"] = adf("edited old comment")
    src = source(client)
    pipeline.run(src)
    assert "edited old comment" in str(loaded(pipeline, src))
    assert "old comment\n" not in str(loaded(pipeline, src))
    client.issues["1"]["fields"]["description"] = adf("new issue content")
    client.issues["1"]["fields"]["updated"] = "2026-10-21T07:01:00.000+0000"
    client.changed = {"1"}
    src = source(client)
    pipeline.run(src)
    assert "new issue content" in str(loaded(pipeline, src))
    assert "Description 2" in str(loaded(pipeline, src))
    before = copy.deepcopy(pipeline.state["sources"])
    client.issues = {}
    with pytest.raises(Exception, match="disappeared"):
        pipeline.run(source(client))
    assert pipeline.state["sources"] == before
    assert len(loaded(pipeline, src)) == 5
    src = source(client, confirmed_removed=[row[0] for row in loaded(pipeline, src)])
    pipeline.run(src)
    assert loaded(pipeline, src) == []
    pipeline.run(source(client))
    assert loaded(pipeline, src) == []


def test_late_failure_and_owner_change_preserve_state(pipeline):
    client = Reader()
    src = source(client)
    pipeline.run(src)
    before = copy.deepcopy(pipeline.state["sources"])
    rows = loaded(pipeline, src)
    client.fail = lambda p, q: p.endswith("/comment") and q.get("startAt") == 1
    with pytest.raises(Exception, match="Synthetic failure"):
        pipeline.run(source(client))
    assert pipeline.state["sources"] == before and loaded(pipeline, src) == rows
    client.fail = None
    client.owner = "different-owner"
    with pytest.raises(Exception, match="account changed"):
        pipeline.run(source(client))
    assert pipeline.state["sources"] == before and loaded(pipeline, src) == rows


def test_child_removal_confirmation_and_visibility(pipeline):
    client = Reader()
    src = source(client)
    pipeline.run(src)
    client.comment_items.pop(0)
    with pytest.raises(Exception, match="issue:1:comment:11"):
        pipeline.run(source(client))
    pipeline.run(source(client, confirmed_removed=["issue:1:comment:11"]))
    assert len(loaded(pipeline, src)) == 4
    client.comment_items.insert(0, {"id": "11", "body": adf("returned")})
    with pytest.raises(Exception, match="currently visible"):
        pipeline.run(source(client, confirmed_removed=["issue:1:comment:11"]))
    pipeline.run(source(client))
    client.issues = {}
    pipeline.run(source(client, missing_policy="mirror_visibility"))
    assert not loaded(pipeline, src)


def test_load_replay_then_fresh(pipeline):
    client = Reader()
    src = source(client)
    pipeline.run(src)
    client.comment_items[0]["body"] = adf("pending change")
    with patch.object(pipeline, "load", side_effect=RuntimeError("load failed")):
        with pytest.raises(RuntimeError):
            pipeline.run(source(client))
    assert pipeline.has_pending_data
    client.issues = {}
    src = source(client, missing_policy="mirror_visibility")
    pipeline.run(src)
    assert src.cognee_sync_stats["extractions"] == 0
    assert len(loaded(pipeline, src)) == 5
    pipeline.run(source(client, missing_policy="mirror_visibility"))
    assert not loaded(pipeline, src)


@pytest.mark.parametrize(
    "response",
    [
        {"issues": [], "warnings": ["truncated"]},
        {"issues": [], "warnings": None},
        {"issues": [], "isLast": "false"},
        {"issues": [], "isLast": False},
        {"issues": [], "isLast": True, "nextPageToken": "a"},
        {"issues": [], "nextPageToken": ""},
    ],
)
def test_invalid_search_pages(response):
    class Pages:
        def json(self, *a):
            return response

    with pytest.raises(ValueError):
        list(get_paginated_data(Pages(), 1, "search", "issues", use_cursor_pagination=True))


def test_empty_continuation_and_repeated_tokens():
    class Pages:
        def __init__(self):
            self.n = 0

        def json(self, *a):
            self.n += 1
            return {
                "issues": [],
                **({"nextPageToken": "next"} if self.n == 1 else {"isLast": True}),
            }

    assert list(get_paginated_data(Pages(), 1, "search", "issues", use_cursor_pagination=True)) == [
        [],
        [],
    ]

    class Repeated:
        def json(self, *a):
            return {"issues": [], "nextPageToken": "same"}

    with pytest.raises(ValueError, match="repeated"):
        list(get_paginated_data(Repeated(), 1, "search", "issues", use_cursor_pagination=True))


@pytest.mark.parametrize("value", [True, 0, "0", "-1", "1/../../x", "١", "01"])
def test_invalid_numeric_id(value):
    with pytest.raises(ValueError):
        numeric_id(value)


@pytest.mark.parametrize(
    "url",
    [
        "http://x.atlassian.net",
        "https://x.atlassian.net.evil.test",
        "https://u:p@x.atlassian.net",
        "https://x.atlassian.net/path",
        "https://x.atlassian.net:443",
    ],
)
def test_bad_site(url):
    with pytest.raises(ValueError):
        site_root(url)


def test_adf_context_and_text_extraction():
    value = {
        "type": "doc",
        "content": [
            {
                "type": "expand",
                "attrs": {"title": "Policy"},
                "content": [
                    {
                        "type": "orderedList",
                        "attrs": {"order": 3},
                        "content": [
                            {
                                "type": "listItem",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "retry"}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"alt": "diagram"}}]},
        ],
    }
    assert "Policy\n3. retry" in adf_text(value)
    assert "diagram" in adf_text(value)
    assert extract_text(b"hello", "text/plain") == "hello"
    with pytest.raises(ValueError):
        extract_text(b"\xff", "text/plain")
    with pytest.raises(ValueError):
        extract_text(b"invalid PDF", "application/pdf")
    with pytest.raises(ValueError):
        extract_text(b"12345", "text/plain", max_chars=4)


def test_timezone_window_overlap():
    query = window(
        "project = KAN",
        "2026-10-21T07:00:59+00:00",
        datetime(2026, 10, 21, 7, 3, 30, tzinfo=UTC),
        "Asia/Kolkata",
    )
    assert "2026-10-21 12:25" in query and "2026-10-21 12:34" in query


def test_changed_context_and_force_refresh(pipeline):
    client = Reader()
    src = source(client)
    pipeline.run(src)
    client.issues["1"]["fields"]["project"]["name"] = "Renamed project"
    client.issues["1"]["fields"]["description"] = adf("silent description")
    pipeline.run(source(client))
    assert "Renamed project" in str(loaded(pipeline, src))
    assert "silent description" not in str(loaded(pipeline, src))
    pipeline.run(source(client, force_refresh=True))
    assert "silent description" in str(loaded(pipeline, src))


def test_rich_cards_and_struck_text_survive_public_factory(pipeline):
    client = Reader()
    client.issues["1"]["fields"]["description"] = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "blockCard", "attrs": {"url": "https://example.test/critical-procedure"}},
            {"type": "embedCard", "attrs": {"url": "https://example.test/incident-lesson"}},
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Do not rotate keys", "marks": [{"type": "strike"}]}
                ],
            },
        ],
    }
    src = source(client)
    pipeline.run(src)
    text = str(loaded(pipeline, src))
    assert "https://example.test/critical-procedure" in text
    assert "https://example.test/incident-lesson" in text
    assert "~~Do not rotate keys~~" in text


def test_inventory_budget_stops_before_continuation():
    from cognee_community_connector_jira.snapshot import inventory

    client = Reader()
    with pytest.raises(ValueError, match="inventory budget"):
        inventory(client, 'project in ("KAN")', [], 1, 200, 1)
    # Two bounded pages are enough to discover the exceeded configured limit.
    assert len([p for p, q in client.calls if p.endswith("search/jql")]) == 2


def test_increasing_attachment_limit_retries_prior_omission(pipeline):
    client = Reader()
    src = source(client, max_attachment_bytes=4)
    pipeline.run(src)
    assert "exceeds configured byte limit" in str(loaded(pipeline, src))
    client.calls.clear()
    src = source(client, max_attachment_bytes=8)
    pipeline.run(src)
    assert "hello" in str(loaded(pipeline, src))
    assert any(p == "attachment" for p, q in client.calls)
    assert "exceeds configured byte limit" not in str(loaded(pipeline, src))


def test_removal_policy_is_explicit_and_automatic_workflow_cleans_each_type(pipeline):
    client = Reader()
    with pytest.raises(TypeError, match="missing_policy"):
        jira_source(
            client.site_url, "owner@example.test", "token", ["KAN"], resource_name="explicit"
        )

    def automatic():
        return jira_source(
            client.site_url,
            "owner@example.test",
            "token",
            ["KAN"],
            resource_name="explicit",
            missing_policy="mirror_visibility",
            client=client,
        )

    src = automatic()
    pipeline.run(src)
    assert len(loaded(pipeline, src)) == 5
    client.comment_items.pop(0)
    pipeline.run(automatic())
    assert len(loaded(pipeline, src)) == 4
    client.files = []
    pipeline.run(automatic())
    assert len(loaded(pipeline, src)) == 3
    client.issues.pop("1")
    pipeline.run(automatic())
    assert len(loaded(pipeline, src)) == 1
    client.issues = {}
    pipeline.run(automatic())
    assert not loaded(pipeline, src)


@pytest.mark.parametrize(
    "mark,marked_text,expected", [("sup", "-3", "10^{-3}"), ("sub", "2", "H_{2}")]
)
def test_subsup_meaning_changes_staged_content(pipeline, mark, marked_text, expected):
    client = Reader()
    prefix = "Error budget: 10" if mark == "sup" else "Formula: H"

    def description(marked):
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": prefix},
                        {
                            "type": "text",
                            "text": marked_text,
                            **(
                                {"marks": [{"type": "subsup", "attrs": {"type": mark}}]}
                                if marked
                                else {}
                            ),
                        },
                    ],
                }
            ],
        }

    client.issues["1"]["fields"]["description"] = description(True)
    src = source(client)
    pipeline.run(src)
    marked_rows = loaded(pipeline, src)
    assert expected in str(marked_rows)
    client.issues["1"]["fields"]["description"] = description(False)
    client.issues["1"]["fields"]["updated"] = "2026-10-21T07:05:00.000+0000"
    client.changed = {"1"}
    pipeline.run(source(client))
    assert marked_rows != loaded(pipeline, src)


@pytest.mark.parametrize("attrs", [None, {}, {"type": "invalid"}, {"type": True}])
def test_invalid_subsup_aborts(attrs):
    with pytest.raises(ValueError, match="superscript/subscript"):
        adf_text({"type": "text", "text": "3", "marks": [{"type": "subsup", "attrs": attrs}]})


class ReconciledScopeReader(Reader):
    """General search stays stale while known-ID reconciliation is current."""

    def __init__(self):
        super().__init__()
        self.excluded = set()

    def json(self, path, params=None):
        if path.endswith("search/jql") and (params or {}).get("reconcileIssues"):
            stale = self.issues
            self.issues = {key: value for key, value in stale.items() if key not in self.excluded}
            try:
                return super().json(path, params)
            finally:
                self.issues = stale
        return super().json(path, params)


@pytest.mark.parametrize("policy", ["mirror_visibility", "require_confirmation"])
def test_reconciled_exclusion_overrides_stale_membership(pipeline, policy):
    client = ReconciledScopeReader()

    def sync(**kwargs):
        return source(client, jql='labels = "synthetic"', missing_policy=policy, **kwargs)

    src = sync()
    pipeline.run(src)
    initial = loaded(pipeline, src)
    assert len(initial) == 5
    client.excluded = {"1"}
    if policy == "require_confirmation":
        state = copy.deepcopy(pipeline.state["sources"])
        with pytest.raises(Exception, match="disappeared"):
            pipeline.run(sync())
        assert loaded(pipeline, src) == initial
        assert pipeline.state["sources"] == state
        pipeline.run(sync(confirmed_removed=[row[0] for row in initial if row[0] != "issue:2"]))
    else:
        pipeline.run(sync())
    assert [row[0] for row in loaded(pipeline, src)] == ["issue:2"]
    # A final excluded issue must publish an authoritative empty snapshot too.
    client.excluded = {"1", "2"}
    pipeline.run(sync(confirmed_removed=["issue:2"] if policy == "require_confirmation" else []))
    assert loaded(pipeline, src) == []
    pipeline.run(sync())
    assert loaded(pipeline, src) == []  # Stale discovery cannot resurrect removed IDs.


def test_reconciliation_exclusions_are_scoped_to_each_complete_batch():
    from cognee_community_connector_jira.snapshot import inventory

    client = ReconciledScopeReader()
    template = client.issues["2"]
    client.issues = {}
    for identity in range(1, 53):
        item = copy.deepcopy(template)
        item.update(id=str(identity), key=f"KAN-{identity}")
        client.issues[str(identity)] = item
    client.excluded = {"1", "51"}
    result = inventory(client, 'project = "KAN"', [str(i) for i in range(1, 52)], 10, 20, 100)
    assert set(result) == set(client.issues) - client.excluded
    assert "52" in result  # Newly discovered IDs outside reconciliation batches survive.
    batches = {
        tuple(params["reconcileIssues"])
        for path, params in client.calls
        if params.get("reconcileIssues")
    }
    assert sorted(map(len, batches)) == [2, 50]


def test_reconciliation_failure_withholds_scope_removal(pipeline):
    client = ReconciledScopeReader()
    src = source(client, missing_policy="mirror_visibility")
    pipeline.run(src)
    initial, state = loaded(pipeline, src), copy.deepcopy(pipeline.state["sources"])
    client.excluded = {"1"}
    client.fail = lambda path, params: bool(params.get("reconcileIssues"))
    with pytest.raises(Exception, match="Synthetic failure"):
        pipeline.run(source(client, missing_policy="mirror_visibility"))
    assert loaded(pipeline, src) == initial
    assert pipeline.state["sources"] == state


@pytest.mark.parametrize(
    "jql",
    [
        'summary ~ "order by"',
        "summary ~ 'ORDER BY'",
        'status = "Order By"',
        r'summary ~ "say \"order by\" now"',
        r"summary ~ 'say \'order by\' now'",
        'summary ~ "order" AND summary ~ "by"',
    ],
)
def test_jql_ordering_words_inside_literals_are_allowed(jql):
    client = Reader()
    source(client, jql=jql)
    assert not client.calls


@pytest.mark.parametrize(
    "jql",
    [
        'summary ~ "order by" ORDER BY updated',
        "status = 'open' oRdEr\tBy key",
        r'summary ~ "say \"order by\" now" ORDER BY key',
        'summary ~ "unterminated order by',
    ],
)
def test_jql_actual_ordering_or_unterminated_literals_are_rejected(jql):
    with pytest.raises(ValueError, match="jql"):
        source(Reader(), jql=jql)
