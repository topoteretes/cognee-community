import copy

import pytest

from cognee_community_connector_mixpanel.documents import document
from cognee_community_connector_mixpanel.errors import ResponseContractError


def render(kind, item, scope="scope", sensitive=False):
    return document(kind, item, scope, "https://mixpanel.com/project/123", sensitive)


def test_reports_preserve_step_order_and_ignore_polling_counts(report):
    row = render("report", report)
    assert "Signup" in row["content"] and "Purchase" in row["content"]
    assert render("report", {**report, "total_view_count": 200, "modified": "now"}) == row
    changed = copy.deepcopy(report)
    changed["params"]["events"].reverse()
    assert render("report", changed) != row


def test_schema_exact_identity_property_map_and_contact_exclusion(schema):
    row = render("schema", schema)
    assert "Billing plan" in row["content"]
    assert "owner@example.com" not in row["content"]
    assert "examples" not in row["content"]
    assert render("schema", {**schema, "name": "signup"})["id"] != row["id"]
    assert render("schema", {**schema, "name": "Sign up"})["id"] != row["id"]
    assert render("schema", schema, "other")["id"] != row["id"]
    reordered = copy.deepcopy(schema)
    reordered["schemaJson"]["metadata"]["com.mixpanel"]["tags"].reverse()
    assert render("schema", reordered) == row


def test_cohort_rules_included_membership_counts_excluded(cohort):
    row = render("cohort", cohort)
    assert "bhvr_0" in row["content"] and "Purchase" in row["content"]
    assert render("cohort", {**cohort, "count": 1000}) == row
    groups = {
        "id": 3,
        "name": "Segment",
        "groups": [{"operator": "or", "conditions": [{"event": "Signup"}, {"event": "Purchase"}]}],
    }
    assert '"operator": "or"' in render("cohort", groups)["content"]


@pytest.mark.parametrize(
    "kind,item",
    [
        ("report", {"id": 1, "name": "Only metadata", "type": "insights"}),
        ("cohort", {"id": 1, "name": "Only metadata", "count": 10}),
        ("schema", {"entityType": "event", "name": "Signup", "schemaJson": {"properties": []}}),
        ("report", {"id": True, "name": "Bad ID", "type": "insights", "params": {"events": []}}),
        (
            "report",
            {
                "id": 1,
                "name": "Unknown shape",
                "type": "insights",
                "params": {"new_unknown_parameter": []},
            },
        ),
    ],
)
def test_unsupported_or_missing_definition_stops_batch(kind, item):
    with pytest.raises(ResponseContractError):
        render(kind, item)


def test_sensitive_literals_redacted_with_explicit_opt_in(report):
    report["params"]["filters"] = [{"property": "email", "value": "person@example.com"}]
    row = render("report", report)
    assert "person@example.com" not in row["content"]
    assert "redacted" in row["content"]
    assert "person@example.com" in render("report", report, sensitive=True)["content"]


def test_installed_sdk_bookmark_builder_definition(report):
    from mixpanel_headless.workspace import Workspace

    # This pure builder uses no instance state and makes no provider request.
    params = Workspace._build_query_params(
        None,
        events=["Signup"],
        math="total",
        math_property=None,
        per_user=None,
        from_date=None,
        to_date=None,
        last=30,
        unit="day",
        group_by=None,
        where=None,
        formulas=[],
        rolling=None,
        cumulative=False,
        mode="timeseries",
    )
    assert "sections" in params and "displayOptions" in params
    report["params"] = params
    row = render("report", report)
    assert "Signup" in row["content"] and '"show"' in row["content"]
    assert render("report", {**report, "params": {**params, "modified": "later"}}) == row


def test_sensitive_property_and_literal_in_separate_sibling_nodes(cohort):
    cohort["selector"] = {
        "operator": "and",
        "children": [
            {
                "operator": "==",
                "children": [
                    {"property": {"source": "user", "name": "phone"}},
                    {"literal": {"value": "+15551234567"}},
                ],
            },
            {"property": "plan", "value": "enterprise"},
        ],
    }
    content = render("cohort", cohort)["content"]
    assert "+15551234567" not in content
    assert "enterprise" in content and "phone" in content
    assert "+15551234567" in render("cohort", cohort, sensitive=True)["content"]


def test_sensitive_schema_enum_and_opaque_selector(schema, cohort):
    schema["schemaJson"]["properties"]["account_id"] = {"type": "string", "enum": ["acct_123"]}
    assert "acct_123" not in render("schema", schema)["content"]
    assert "acct_123" in render("schema", schema, sensitive=True)["content"]
    cohort["selector"] = 'user["phone"] == "+15551234567"'
    assert "+15551234567" not in render("cohort", cohort)["content"]


def test_renderer_version_changes_semantic_row(report, monkeypatch):
    import cognee_community_connector_mixpanel.documents as documents

    old = render("report", report)
    monkeypatch.setattr(documents, "RENDERER_VERSION", 2)
    assert render("report", report) != old


def test_sdk_bookmark_filter_literal_is_redacted_property_preserved(report):
    from mixpanel_headless import Filter
    from mixpanel_headless._internal.bookmark_builders import build_filter_entry

    report["params"]["filters"] = [build_filter_entry(Filter.equals("phone", "+15551234567"))]
    row = render("report", report)
    assert "+15551234567" not in row["content"]
    assert '"value": "phone"' in row["content"]
    assert "+15551234567" in render("report", report, sensitive=True)["content"]
