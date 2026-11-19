"""What each GA4 definition looks like as a document."""

from fake_ga4 import FakeGA4

from cognee_community_connector_ga4.ga4 import (
    render_annotation,
    render_audience,
    render_custom_dimension,
    render_custom_metric,
    render_key_event,
)

BASE = "properties/123456789"


def test_a_custom_dimension_names_its_parameter_scope_and_meaning():
    fake = FakeGA4()
    dimension = fake.add_custom_dimension(
        "11",
        "plan_tier",
        "Plan tier",
        scope="USER",
        description="Billing plan of the account.",
        disallowAdsPersonalization=True,
    )

    row = render_custom_dimension(dimension)

    assert row["id"] == f"{BASE}/customDimensions/11"
    assert row["title"] == "Plan tier"
    assert row["content"] == (
        "Custom dimension: plan_tier\n"
        "Scope: user\n"
        "Ads personalization: excluded\n"
        "\n"
        "Billing plan of the account."
    )
    assert row["_deleted"] is False


def test_a_custom_metric_names_its_unit_and_restricted_data():
    fake = FakeGA4()
    metric = fake.add_custom_metric(
        "21",
        "order_margin",
        "Order margin",
        measurementUnit="CURRENCY",
        restrictedMetricType=["REVENUE_DATA", "COST_DATA"],
    )

    content = render_custom_metric(metric)["content"]

    assert "Custom metric: order_margin" in content
    assert "Unit: currency" in content
    assert "Restricted data: cost data, revenue data" in content


def test_a_key_event_names_how_it_is_counted_and_its_default_value():
    fake = FakeGA4()
    event = fake.add_key_event(
        "31",
        "purchase",
        countingMethod="ONCE_PER_SESSION",
        custom=False,
        defaultValue={"numericValue": 9.99, "currencyCode": "EUR"},
    )

    row = render_key_event(event)

    assert row["title"] == "purchase"
    assert row["content"] == (
        "Key event: purchase\n"
        "Counted: once per session\n"
        "Default value: 9.99 EUR\n"
        "Event: Google Analytics default"
    )


def test_an_audience_carries_its_rules_but_not_its_creation_time():
    fake = FakeGA4()
    audience = fake.add_audience(
        "41",
        "Purchasers",
        "Users who bought something.",
        eventTrigger={"eventName": "joined_purchasers", "logCondition": "AUDIENCE_JOINED"},
    )

    content = render_audience(audience)["content"]

    assert "Membership duration: 30 days" in content
    assert "Logs event: joined_purchasers (audience joined)" in content
    assert "Users who bought something." in content
    assert "Include users (across all sessions): event purchase" in content
    assert "2026-10-01" not in content
    # exclusion only means something when a clause excludes
    assert "Exclusion" not in content


def test_an_excluding_audience_says_how_long_the_exclusion_lasts():
    fake = FakeGA4()
    audience = fake.add_audience("42", "Not churned", "Users who did not cancel.")
    audience["filterClauses"].append({"clauseType": "EXCLUDE", "simpleFilter": {"scope": "X"}})

    assert "Exclusion: exclude temporarily" in render_audience(audience)["content"]


def test_the_order_of_audience_clauses_does_not_change_the_document():
    fake = FakeGA4()
    audience = fake.add_audience("43", "Mixed", "Two clauses.")
    audience["filterClauses"].append({"clauseType": "EXCLUDE", "simpleFilter": {"scope": "X"}})
    reordered = {**audience, "filterClauses": list(reversed(audience["filterClauses"]))}

    assert render_audience(audience) == render_audience(reordered)


def test_a_default_audience_without_clauses_keeps_its_name_and_description():
    fake = FakeGA4()
    audience = fake.add_audience("44", "All Users", "All users", filterClauses=[])

    row = render_audience(audience)

    assert row["title"] == "All Users"
    assert "Definition" not in row["content"]
    assert "All users" in row["content"]


def test_an_annotation_names_its_date_or_date_range():
    fake = FakeGA4()
    single = fake.add_annotation("51", "Checkout redesign shipped", description="New flow.")
    span = fake.add_annotation(
        "52",
        "Spring sale",
        annotationDate=None,
        annotationDateRange={
            "startDate": {"year": 2026, "month": 3, "day": 1},
            "endDate": {"year": 2026, "month": 3, "day": 7},
        },
        systemGenerated=True,
    )

    assert render_annotation(single)["content"] == ("Annotation date: 2026-10-01\n\nNew flow.")
    assert render_annotation(span)["content"] == (
        "Annotation date: 2026-03-01 to 2026-03-07\nAdded by: Google Analytics"
    )


def test_a_definition_without_a_display_name_falls_back_to_its_parameter():
    fake = FakeGA4()
    dimension = fake.add_custom_dimension("12", "region", "")

    assert render_custom_dimension(dimension)["title"] == "region"


def test_unspecified_enums_are_left_out():
    fake = FakeGA4()
    metric = fake.add_custom_metric(
        "22", "score", "Score", measurementUnit="MEASUREMENT_UNIT_UNSPECIFIED"
    )

    assert "Unit" not in render_custom_metric(metric)["content"]


def _rules(*clauses) -> list[str]:
    fake = FakeGA4()
    audience = fake.add_audience("49", "Rules", "Rules.", filterClauses=list(clauses))
    content = render_audience(audience)["content"]
    return content.split("Definition:\n", 1)[1].splitlines()


def _filter(*expressions, joiner="andGroup") -> dict:
    return {joiner: {"filterExpressions": list(expressions)}}


def _condition(**spec) -> dict:
    return {"dimensionOrMetricFilter": spec}


# the rules of an audience made in the GA4 interface, as the live Admin API returned them
_MORE_THAN_ONE = _condition(
    atAnyPointInTime=True,
    fieldName="event_count",
    numericFilter={"operation": "GREATER_THAN", "value": {"int64Value": "1"}},
)
_IN_APP_PURCHASE = {
    "eventFilter": {
        "eventName": "in_app_purchase",
        "eventParameterFilterExpression": _filter(_filter(_MORE_THAN_ONE, joiner="orGroup")),
    }
}
LIVE_REPEAT_BUYERS = {
    "clauseType": "INCLUDE",
    "simpleFilter": {
        "scope": "AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS",
        "filterExpression": _filter(_filter(_IN_APP_PURCHASE, joiner="orGroup")),
    },
}


def test_a_live_audience_definition_reads_as_one_sentence():
    assert _rules(LIVE_REPEAT_BUYERS) == [
        "Include users (across all sessions): "
        "event in_app_purchase where event_count > 1 at any point in time"
    ]


def test_each_kind_of_condition_reads_as_words():
    clause = {
        "clauseType": "INCLUDE",
        "simpleFilter": {
            "scope": "AUDIENCE_FILTER_SCOPE_WITHIN_SAME_SESSION",
            "filterExpression": _filter(
                _condition(
                    fieldName="page_type",
                    stringFilter={"matchType": "CONTAINS", "value": "pricing"},
                ),
                _condition(fieldName="country", inListFilter={"values": ["FR", "DE"]}),
                _condition(
                    fieldName="purchase_revenue",
                    betweenFilter={
                        "fromValue": {"doubleValue": 10.5},
                        "toValue": {"int64Value": "99"},
                    },
                    inAnyNDayPeriod=7,
                ),
                {
                    "notExpression": _condition(
                        fieldName="plan_tier",
                        stringFilter={"matchType": "EXACT", "value": "Free", "caseSensitive": True},
                    )
                },
            ),
        },
    }

    assert _rules(clause) == [
        "Include users (within same session): ("
        "country is one of DE, FR"
        " and not plan_tier is 'Free' (case sensitive)"
        " and page_type contains 'pricing'"
        " and purchase_revenue between 10.5 and 99 in any 7-day period)"
    ]


def test_an_or_group_and_an_exclude_clause_read_as_words():
    clause = {
        "clauseType": "EXCLUDE",
        "simpleFilter": {
            "scope": "AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS",
            "filterExpression": _filter(
                _filter(
                    {"eventFilter": {"eventName": "refund"}},
                    {"eventFilter": {"eventName": "chargeback"}},
                    joiner="orGroup",
                )
            ),
        },
    }

    assert _rules(clause) == [
        "Exclude users (across all sessions): (event chargeback or event refund)"
    ]


def test_a_sequence_reads_as_ordered_steps():
    clause = {
        "clauseType": "INCLUDE",
        "sequenceFilter": {
            "scope": "AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS",
            "sequenceMaximumDuration": "604800s",
            "sequenceSteps": [
                {"filterExpression": _filter({"eventFilter": {"eventName": "view_item"}})},
                {
                    "immediatelyFollows": True,
                    "filterExpression": _filter({"eventFilter": {"eventName": "add_to_cart"}}),
                },
                {
                    "constraintDuration": "3600s",
                    "filterExpression": _filter({"eventFilter": {"eventName": "purchase"}}),
                },
            ],
        },
    }

    assert _rules(clause) == [
        "Include users who in order (across all sessions): event view_item, "
        "directly then event add_to_cart, then event purchase within 3600s, all within 604800s"
    ]


def test_a_filter_this_version_does_not_know_is_kept_as_data():
    clause = {
        "clauseType": "INCLUDE",
        "simpleFilter": {
            "scope": "AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS",
            "filterExpression": _filter({"futureFilter": {"x": 1}}),
        },
    }

    assert _rules(clause) == ['Include users (across all sessions): {"futureFilter": {"x": 1}}']
