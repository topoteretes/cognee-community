"""Unit tests for the Klaviyo connector.

The account is mocked by ``FakeKlaviyo`` (see ``fake_klaviyo.py``), so no key
and no network are needed. Four layers:

* pure tests for rendering, HTML to text and template-tag cleanup;
* the HTTP layer: headers, retries, waits, error messages, broken pages;
* ``sync_account`` driven with a plain dict as state (backfill, cursor,
  editable campaigns, template edits, deletion, and the failure paths that
  must not delete anything);
* a real ``dlt`` pipeline into a temp sqlite destination, plus the factory's
  wiring (document-source tag, scope, stats, merge schema).
"""

from __future__ import annotations

import dlt
import pytest
import requests
from cognee.tasks.ingestion.dlt_utils import NODE_SET_COLUMN, document_source_tag
from fake_klaviyo import FakeKlaviyo, Resp, delay_step, email_step, error, sms_step, stamp

from cognee_community_connector_klaviyo import klaviyo as kl
from cognee_community_connector_klaviyo import klaviyo_source
from cognee_community_connector_klaviyo.klaviyo import (
    API_REVISION,
    KlaviyoAPIError,
    KlaviyoResponseError,
    _Client,
    _ordered_actions,
    clean_template_tags,
    html_to_text,
    render_campaign,
    render_flow,
    render_template,
    sync_account,
)

KEY = "pk_SECRETKEY0123456789abcdef"
ALL = ["campaigns", "flows", "templates"]

WELCOME_HTML = (
    "<html><head><style>p{color:red}</style><title>x</title></head><body>"
    "<table><tr><td><h1>Welcome, {{ first_name|default:'friend' }}!</h1></td></tr>"
    "<tr><td><p>Here is 10% off your first order.</p></td></tr></table>"
    "{% unsubscribe 'Unsubscribe' %}</body></html>"
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Record waits instead of sleeping, and take the jitter out of backoff."""
    slept: list[float] = []
    monkeypatch.setattr(kl.time, "sleep", slept.append)
    monkeypatch.setattr(kl.random, "random", lambda: 0.0)
    return slept


def client(fake: FakeKlaviyo, **kw) -> _Client:
    return _Client(fake, KEY, **kw)


def account() -> FakeKlaviyo:
    fake = FakeKlaviyo()
    fake.add_template("T1", "Welcome email", html=WELCOME_HTML)
    fake.add_template("T2", "Fall sale", text="Everything is 20% off until Sunday.")
    fake.add_campaign(
        "C1", "Fall Sale", subject="20% off everything", preview="Ends Sunday", template_id="T2"
    )
    fake.add_campaign(
        "C2", "Holiday teaser", status="draft", subject="Coming soon", template_id="T1"
    )
    fake.add_campaign("C3", "Flash SMS", channel="sms", body="Flash sale today! {{ first_name }}")
    fake.add_flow(
        "F1",
        "Welcome series",
        [
            email_step("a1", "Welcome", "Welcome to Brand", "T1", nxt="a2"),
            delay_step("a2", 2, "days", nxt="a3"),
            sms_step("a3", "Still thinking it over? Reply STOP to opt out."),
        ],
    )
    return fake


def sync(fake, state, **kw):
    kw.setdefault("resources", ALL)
    stats: dict = {}
    rows = list(sync_account(client(fake), state, stats=stats, **kw))
    return rows, stats


def by_id(rows):
    return {row["id"]: row for row in rows}


def requested(fake, path):
    return [query for call_path, query in fake.calls if call_path == path]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_html_to_text_keeps_the_copy_and_drops_markup_styles_and_tags():
    text = html_to_text(WELCOME_HTML)
    assert text == "Welcome, {{ first_name }}!\nHere is 10% off your first order."


def test_clean_template_tags_keeps_variable_names_only():
    assert clean_template_tags("Hi {{ person.first_name|default:'there'|title }}") == (
        "Hi {{ person.first_name }}"
    )
    assert clean_template_tags("a{% if x %}b{% endif %}c") == "abc"


def test_render_campaign_has_the_copy_and_no_addresses():
    fake = account()
    campaign = fake._campaign("C1", {})
    message = fake._message("C1-m1", {})
    variations = {"C1-m1": fake._variations("C1-m1", {})}
    row = render_campaign(campaign, [message], variations, {"T2": fake._template("T2", {})})
    content = row["content"]
    assert row["id"] == "campaign:C1"
    assert row["title"] == "Campaign: Fall Sale"
    assert row[NODE_SET_COLUMN] == ["campaigns"]
    for expected in (
        "Status: sent",
        "Send time: 2026-10-02T15:00:00+00:00",
        "Channel: email",
        "Subject: 20% off everything",
        "Preview text: Ends Sunday",
        "From: Brand",
        "Everything is 20% off until Sunday.",
    ):
        assert expected in content
    assert "@" not in content  # sender, reply-to: contact data never stored
    assert "Variation:" not in content  # a single variation is not labelled


def test_render_campaign_labels_ab_variations_and_renders_sms_inline():
    fake = account()
    fake.variations["C3-v2"] = {"name": "Variation B", "details": {"channel": "sms", "body": "B!"}}
    fake.messages["C3-m1"]["variations"].append("C3-v2")
    row = render_campaign(
        fake._campaign("C3", {}),
        [fake._message("C3-m1", {})],
        {"C3-m1": fake._variations("C3-m1", {})},
        {},
    )
    assert "Variation: Variation A" in row["content"]
    assert "Body:\nFlash sale today! {{ first_name }}" in row["content"]
    assert "Body:\nB!" in row["content"]


def test_render_flow_walks_the_steps_in_order():
    fake = account()
    flow = fake._flow("F1", {"additional-fields[flow]": "definition"})
    actions = _ordered_actions(flow["attributes"]["definition"])
    row = render_flow(flow, actions, {"T1": fake._template("T1", {})})
    content = row["content"]
    assert row["id"] == "flow:F1"
    assert "Status: live\nTrigger: Added to List" in content
    assert "1. Email: Welcome\n   Subject: Welcome to Brand" in content
    assert "Welcome, {{ first_name }}!" in content
    assert "2. Wait 2 days" in content
    assert "3. SMS: SMS" in content and "Reply STOP to opt out." in content


def test_ordered_actions_follow_links_and_keep_unreached_ones():
    definition = {
        "entry_action_id": "s",
        "actions": [
            {"id": "orphan", "type": "send-sms"},
            {"id": "no", "type": "send-sms"},
            {"id": "yes", "type": "send-email"},
            {
                "id": "s",
                "type": "conditional-split",
                "links": {"next_if_true": "yes", "next_if_false": "no"},
            },
        ],
    }
    assert [a["id"] for a in _ordered_actions(definition)] == ["s", "yes", "no", "orphan"]


def test_ab_test_steps_render_their_main_message():
    action = {
        "id": "x",
        "type": "ab-test",
        "data": {"main_action": email_step("m", "Promo", "Hello", "T9")},
    }
    row = render_flow({"id": "F", "attributes": {"name": "AB"}}, [action], {})
    assert "1. A/B test of email: Promo" in row["content"]
    assert "Subject: Hello" in row["content"]
    assert kl._flow_template_ids([action]) == ["T9"]


def test_webhook_steps_never_render_their_payload():
    action = {
        "id": "w",
        "type": "send-webhook",
        "data": {"message": {"name": "CRM", "url": "https://x.test", "body": '{"secret": 1}'}},
    }
    row = render_flow({"id": "F", "attributes": {"name": "Hook"}}, [action], {})
    assert "1. Webhook: CRM" in row["content"]
    assert "secret" not in row["content"]


def test_render_template_prefers_the_html_and_falls_back_to_plain_text():
    fake = account()
    # T2 has only a plain-text version
    assert "Everything is 20% off" in render_template(fake._template("T2", {}))["content"]
    welcome = render_template(fake._template("T1", {}))
    assert welcome["title"] == "Email template: Welcome email"
    assert "color:red" not in welcome["content"]
    # Klaviyo keeps the plain-text version apart and does not regenerate it when
    # the HTML is edited: the HTML wins.
    fake.edit_template("T1", text="An outdated plain-text version.")
    content = render_template(fake._template("T1", {}))["content"]
    assert "Welcome, {{ first_name }}!" in content
    assert "outdated" not in content


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_every_request_carries_the_key_and_the_pinned_revision():
    fake = account()
    sync(fake, {})
    assert fake.headers_seen
    for headers in fake.headers_seen:
        assert headers["Authorization"] == f"Klaviyo-API-Key {KEY}"
        assert headers["revision"] == API_REVISION == "2026-10-15"


def test_rate_limits_are_retried_after_the_requested_wait(no_sleep):
    fake = account()
    fake.failures.append(("/campaigns", Resp(429, {}, {"Retry-After": "3"})))
    fake.failures.append(("/flows", Resp(503, {})))
    rows, _ = sync(fake, {})
    assert "campaign:C1" in by_id(rows)
    assert no_sleep == [3.0, 1.0]


def test_a_long_retry_after_is_refused_instead_of_slept(no_sleep):
    fake = account()
    fake.failures.append(("/campaigns", Resp(429, {}, {"Retry-After": "600"})))
    with pytest.raises(KlaviyoAPIError, match="HTTP 429") as caught:
        sync(fake, {})
    assert no_sleep == []
    assert "600" in caught.value.detail


def test_the_run_wait_budget_caps_total_sleep(no_sleep):
    fake = account()
    for _ in range(3):
        fake.failures.append(("/campaigns", Resp(429, {}, {"Retry-After": "50"})))
    with pytest.raises(KlaviyoAPIError):
        sync(fake, {})
    assert sum(no_sleep) <= kl._MAX_TOTAL_WAIT_SECONDS


def test_errors_name_the_status_and_endpoint_but_never_the_key_or_body():
    fake = account()
    fake.failures.append(("/campaigns/C1", error(500, "server_error", f"echo {KEY}")))
    fake.failures.append(("/campaigns/C1", error(400, "invalid", f"echo {KEY}")))
    with pytest.raises(KlaviyoAPIError) as caught:
        client(fake).get("/campaigns/C1", endpoint="/campaigns/{id}")
    message = str(caught.value)
    assert message == "Klaviyo /campaigns/{id}: HTTP 400"
    assert KEY not in message and "C1" not in message
    assert caught.value.detail == "invalid"


def test_network_errors_report_only_their_type(no_sleep):
    class Broken(FakeKlaviyo):
        def get(self, url, params=None, headers=None, timeout=None):
            raise requests.ConnectionError(f"failed with headers {headers}")

    with pytest.raises(KlaviyoAPIError) as caught:
        client(Broken()).get("/campaigns", endpoint="/campaigns")
    assert str(caught.value) == "Klaviyo /campaigns: no answer"
    assert caught.value.detail == "ConnectionError"
    assert KEY not in repr(caught.value)
    assert len(no_sleep) == kl._MAX_RETRIES - 1


def test_a_200_that_is_not_json_raises():
    fake = account()
    fake.failures.append(("/campaigns", Resp(200, text="<html>login</html>")))
    with pytest.raises(KlaviyoResponseError, match="without JSON"):
        sync(fake, {})


def test_listing_follows_next_links_through_every_page():
    fake = account()
    for n in range(4, 30):
        fake.add_campaign(f"C{n}", f"Campaign {n}", subject="s", template_id="T2")
    fake.page_size["campaigns"] = 7
    rows, stats = sync(fake, {}, resources=["campaigns"])
    assert len([r for r in rows if r["id"].startswith("campaign:")]) == 29
    assert stats["listed"] == 29


@pytest.mark.parametrize(
    "body",
    [
        {"links": {}},
        {"data": {"id": "x"}},
        {"data": [{"type": "campaign"}]},
        {"data": [], "included": {}},
        [],
    ],
)
def test_a_listing_of_the_wrong_shape_raises(body):
    fake = account()
    fake.failures.append(("/campaigns", Resp(200, body)))
    with pytest.raises(KlaviyoResponseError):
        sync(fake, {})


def test_a_next_link_off_the_api_host_is_refused():
    fake = account()
    fake.page_size["templates"] = 1
    fake.next_link_host = "evil.test"
    with pytest.raises(KlaviyoResponseError, match="off the API host"):
        sync(fake, {}, resources=["templates"])
    assert all(call[0].startswith("/") for call in fake.calls)


def test_a_repeating_next_link_is_refused():
    fake = account()
    looping = {"data": [], "links": {"next": "https://a.klaviyo.com/api/flows?page%5Bcursor%5D=1"}}
    fake.failures += [("/flows", Resp(200, looping)), ("/flows", Resp(200, looping))]
    with pytest.raises(KlaviyoResponseError, match="repeated"):
        sync(fake, {}, resources=["flows"])


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def test_backfill_renders_every_selected_record():
    fake = account()
    state: dict = {}
    rows, stats = sync(fake, state)
    assert set(by_id(rows)) == {
        "campaign:C1",
        "campaign:C2",
        "campaign:C3",
        "flow:F1",
        "template:T1",
        "template:T2",
    }
    assert stats["rendered"] == 6 and stats["deleted"] == 0
    assert state["campaigns"]["uses"] == {"C1": ["T2"], "C2": ["T1"], "C3": []}
    assert state["flows"]["uses"] == {"F1": ["T1"]}
    assert state["template_cursor"] == kl._stamp(
        kl._parse_time(stamp(10))
    )  # T2, the newest template


def test_a_selection_syncs_only_those_kinds():
    rows, _ = sync(account(), {}, resources=["flows"])
    assert set(by_id(rows)) == {"flow:F1"}


def test_an_unchanged_account_yields_nothing_on_the_next_run():
    fake = account()
    state: dict = {}
    sync(fake, state)
    rows, stats = sync(fake, state)
    assert rows == []
    assert stats["unchanged"] == 1  # the draft campaign is read again, and is unchanged


def test_the_cursor_lists_only_records_updated_since_with_an_overlap():
    fake = account()
    state: dict = {}
    sync(fake, state)
    fake.calls.clear()
    sync(fake, state)
    changed = [q for q in requested(fake, "/campaigns") if "filter" in q]
    cursor = kl._parse_time(state["campaigns"]["cursor"])
    start = kl._stamp(cursor - kl._CURSOR_OVERLAP)
    assert changed[0]["filter"] == f"greater-or-equal(updated_at,{start})"
    assert changed[0]["sort"] == "updated_at"
    flows = [q for q in requested(fake, "/flows") if "filter" in q]
    assert flows[0]["filter"].startswith("greater-or-equal(updated,")


def test_a_renamed_campaign_is_the_only_document_re_rendered():
    fake = account()
    state: dict = {}
    sync(fake, state)
    fake.edit_campaign("C1", name="Fall Sale (extended)")
    rows, _ = sync(fake, state)
    assert set(by_id(rows)) == {"campaign:C1"}
    assert by_id(rows)["campaign:C1"]["title"] == "Campaign: Fall Sale (extended)"


def test_a_draft_edit_is_picked_up_although_the_campaign_did_not_move():
    fake = account()
    state: dict = {}
    sync(fake, state)
    fake.edit_variation("C2", subject="Our biggest holiday sale")
    rows, _ = sync(fake, state)
    assert "Subject: Our biggest holiday sale" in by_id(rows)["campaign:C2"]["content"]


def test_a_sent_campaign_is_not_read_again_until_it_changes():
    fake = account()
    state: dict = {}
    sync(fake, state)
    fake.calls.clear()
    sync(fake, state)
    assert requested(fake, "/campaigns/C1") == []
    assert requested(fake, "/campaigns/C2")  # the draft


def test_a_template_edit_re_renders_the_campaigns_and_flows_that_use_it():
    fake = account()
    state: dict = {}
    sync(fake, state)
    fake.edit_template("T1", html="<p>A brand new welcome.</p>")
    rows, _ = sync(fake, state)
    assert set(by_id(rows)) == {"template:T1", "flow:F1", "campaign:C2"}
    assert "A brand new welcome." in by_id(rows)["flow:F1"]["content"]


def test_template_edits_reach_owners_without_the_templates_kind():
    fake = account()
    state: dict = {}
    sync(fake, state, resources=["flows"])
    assert state["template_cursor"] == kl._stamp(kl._parse_time(stamp(10)))
    fake.edit_template("T1", html="<p>Updated copy.</p>")
    rows, _ = sync(fake, state, resources=["flows"])
    assert set(by_id(rows)) == {"flow:F1"}
    assert "Updated copy." in rows[0]["content"]


def test_templates_selected_later_are_backfilled_in_full():
    fake = account()
    state: dict = {}
    sync(fake, state, resources=["campaigns"])
    rows, _ = sync(fake, state, resources=["campaigns", "templates"])
    assert {"template:T1", "template:T2"} <= set(by_id(rows))


def test_deleted_records_are_tombstoned_after_a_confirming_get():
    fake = account()
    state: dict = {}
    sync(fake, state)
    fake.delete_campaign("C1")
    del fake.flows["F1"]
    del fake.templates["T2"]
    rows, stats = sync(fake, state)
    deleted = {r["id"] for r in rows if r["_deleted"]}
    assert deleted == {"campaign:C1", "flow:F1", "template:T2"}
    assert stats["deleted"] == 3
    assert "C1" not in state["campaigns"]["known"]
    assert "C1" not in state["campaigns"]["uses"]
    assert requested(fake, "/campaigns/C1")  # confirmed with a GET first


def test_archiving_forgets_and_include_archived_keeps():
    fake = account()
    state: dict = {}
    sync(fake, state)
    fake.edit_campaign("C1", archived=True)
    fake.edit_flow("F1", archived=True)
    rows, _ = sync(fake, state)
    assert {r["id"] for r in rows if r["_deleted"]} == {"campaign:C1", "flow:F1"}

    kept: dict = {}
    rows, _ = sync(fake, kept, include_archived=True)
    assert "Archived: yes" in by_id(rows)["campaign:C1"]["content"]


def test_an_id_missing_from_the_listing_but_still_in_scope_is_kept():
    fake = account()
    state: dict = {}
    sync(fake, state, resources=["flows"])
    empty_page = Resp(200, {"data": [], "links": {}})
    fake.failures += [("/flows", Resp(200, {"data": [], "links": {}})), ("/flows", empty_page)]
    rows, stats = sync(fake, state, resources=["flows"])
    assert rows == [] and stats["deleted"] == 0
    assert "F1" in state["flows"]["known"]


def test_a_failed_confirmation_aborts_before_any_row_or_state_change():
    fake = account()
    state: dict = {}
    sync(fake, state)
    before = repr(state)
    fake.delete_campaign("C1")
    fake.failures.append(("/campaigns/C1", error(400, "bad")))
    with pytest.raises(KlaviyoAPIError):
        sync(fake, state)
    assert repr(state) == before


def test_an_emptied_account_forgets_everything_one_confirmed_id_at_a_time():
    fake = account()
    state: dict = {}
    sync(fake, state)
    for cid in list(fake.campaigns):
        fake.delete_campaign(cid)
    fake.flows.clear()
    fake.templates.clear()
    rows, stats = sync(fake, state)
    assert stats["deleted"] == 6
    assert all(r["_deleted"] for r in rows)


def test_a_deselected_resource_is_forgotten():
    fake = account()
    state: dict = {}
    sync(fake, state)
    rows, _ = sync(fake, state, resources=["campaigns", "flows"])
    assert {r["id"] for r in rows if r["_deleted"]} == {"template:T1", "template:T2"}
    assert "templates" not in state


def test_a_record_that_fails_to_load_is_kept_for_the_next_run():
    fake = account()
    state: dict = {}
    fake.failures += [("/flows/F1", error(500))] * kl._MAX_RETRIES
    rows, stats = sync(fake, state)
    assert "flow:F1" not in by_id(rows)
    assert stats["kept"] == 1 and stats["failed"] == 1
    assert state["flows"]["retry"] == ["F1"]
    rows, _ = sync(fake, state)
    assert "flow:F1" in by_id(rows)
    assert state["flows"]["retry"] == []


def test_three_failures_in_a_row_stop_the_run_as_an_outage():
    fake = account()
    fake.failures += [(r"/campaigns/C\d", error(502))] * (kl._MAX_RETRIES * 3)
    with pytest.raises(KlaviyoAPIError, match="HTTP 502"):
        sync(fake, {}, resources=["campaigns"])


def test_lost_access_while_rendering_raises_instead_of_keeping_records():
    fake = account()
    fake.failures.append(("/flows/F1", error(403, "permission_denied")))
    with pytest.raises(KlaviyoAPIError, match="HTTP 403"):
        sync(fake, {})


def test_a_missing_template_leaves_the_body_out_but_keeps_the_campaign():
    fake = account()
    del fake.templates["T2"]
    rows, _ = sync(fake, {}, resources=["campaigns"])
    content = by_id(rows)["campaign:C1"]["content"]
    assert "Subject: 20% off everything" in content
    assert "Body:" not in content


def test_unknown_or_empty_selections_are_refused():
    with pytest.raises(ValueError, match="Unknown Klaviyo resource"):
        sync(account(), {}, resources=["profiles"])
    with pytest.raises(ValueError, match="at least one"):
        sync(account(), {}, resources=[])


# ---------------------------------------------------------------------------
# Factory and a real dlt pipeline
# ---------------------------------------------------------------------------
def source(fake, **kw):
    return klaviyo_source(api_key=KEY, service=fake, **kw)


def test_source_is_a_merge_resource_tagged_for_document_mode():
    resource = source(account())
    schema = resource.compute_table_schema()
    assert schema["write_disposition"] == "merge"
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["columns"]["_deleted"]["hard_delete"] is True
    assert document_source_tag(resource) == "klaviyo"
    assert getattr(resource, kl.dlt_utils.PIPELINE_SCOPE_ATTR) == "klaviyo_documents"
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"api_key": "pk_abc\n"}, "whitespace"),
        ({"api_key": "AbC123"}, "starts with pk_"),
        ({"resource_name": "Bad-Name"}, "snake_case"),
        ({"resource_name": ""}, "non-empty"),
        ({"include_archived": "yes"}, "True or False"),
        ({"resources": ["lists"]}, "Unknown"),
        ({"session": object()}, "not both"),
    ],
)
def test_bad_arguments_are_refused_up_front(kwargs, message):
    args = {"api_key": KEY, "service": account(), **kwargs}
    with pytest.raises(ValueError, match=message) as caught:
        klaviyo_source(**args)
    assert "pk_abc" not in str(caught.value)


def test_a_missing_key_is_refused(monkeypatch):
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="KLAVIYO_API_KEY"):
        klaviyo_source()


def test_the_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("KLAVIYO_API_KEY", KEY)
    rows = list(klaviyo_source(session=account()))
    assert len(rows) == 6


def _pipeline(tmp_path, name="klaviyo_test"):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'dest.db'}"),
        dataset_name="klaviyo",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _table(pipeline):
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id, content FROM klaviyo_documents ORDER BY id")
    return dict(rows)


def test_two_runs_through_dlt_apply_edits_and_hard_deletes(tmp_path):
    fake = account()
    pipeline = _pipeline(tmp_path)
    pipeline.run(source(fake))
    assert set(_table(pipeline)) == {
        "campaign:C1",
        "campaign:C2",
        "campaign:C3",
        "flow:F1",
        "template:T1",
        "template:T2",
    }
    fake.edit_template("T2", text="Now 30% off.")
    fake.delete_campaign("C3")
    resource = source(fake)
    pipeline.run(resource)
    table = _table(pipeline)
    assert "campaign:C3" not in table
    assert "Now 30% off." in table["campaign:C1"]
    assert "Now 30% off." in table["template:T2"]
    assert resource.cognee_sync_stats["deleted"] == 1
    assert resource.cognee_sync_stats["rendered"] == 2
    assert fake.closed is False  # an injected session belongs to the caller


def test_a_second_account_under_the_same_resource_name_is_refused(tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(source(account()))
    with pytest.raises(Exception, match="already syncs another Klaviyo account"):
        pipeline.run(source(FakeKlaviyo(account_id="OTHER")))


def test_check_active_can_stop_a_sync(tmp_path):
    calls = []

    def check():
        calls.append(1)
        if len(calls) > 3:
            raise PermissionError("revoked")

    with pytest.raises(Exception, match="revoked"):
        _pipeline(tmp_path).run(source(account(), check_active=check))


def test_the_cursor_overlap_does_not_fetch_unchanged_records_again():
    fake = account()
    state: dict = {}
    sync(fake, state)
    fake.calls.clear()
    sync(fake, state)
    # F1 and the newest template sit inside the overlap window, but their stamps
    # did not move: no detail request, no owner re-render.
    assert requested(fake, "/flows/F1") == []
    assert requested(fake, "/campaigns/C1") == []
    assert requested(fake, "/templates/T2") == []


def test_check_active_runs_before_every_request():
    fake = account()
    checks = []
    list(sync_account(_Client(fake, KEY, check_active=lambda: checks.append(1)), {}))
    assert len(checks) >= len(fake.calls)


def test_a_flow_without_a_definition_is_read_from_its_flow_actions():
    fake = account()
    fake.flow_definitions = False
    rows, _ = sync(fake, {}, resources=["flows"])
    content = by_id(rows)["flow:F1"]["content"]
    assert "Email: Welcome" in content and "Wait 2 days" in content
    assert requested(fake, "/flows/F1/flow-actions")


def test_a_flow_with_an_empty_definition_needs_no_extra_request():
    fake = account()
    fake.flows["F1"]["definition"]["actions"] = []
    rows, _ = sync(fake, {}, resources=["flows"])
    assert "Steps:" not in by_id(rows)["flow:F1"]["content"]
    assert requested(fake, "/flows/F1/flow-actions") == []


def _flow_with_its_own_copy(fake):
    """A flow whose email uses its own template copy, as Klaviyo makes them."""
    fake.add_template("FT1", "Welcome email", html="<p>Original welcome.</p>", listed=False)
    fake.add_flow("F2", "Browse abandonment", [email_step("b1", "Nudge", "Still looking?", "FT1")])


def test_an_edit_to_a_flow_emails_own_template_copy_is_picked_up():
    fake = account()
    _flow_with_its_own_copy(fake)
    state: dict = {}
    rows, _ = sync(fake, state)
    assert "Original welcome." in by_id(rows)["flow:F2"]["content"]
    assert "template:FT1" not in by_id(rows)  # copies are not library templates
    fake.edit_template("FT1", html="<p>Rewritten welcome.</p>")  # the flow does not move
    rows, _ = sync(fake, state)
    assert set(by_id(rows)) == {"flow:F2"}
    assert "Rewritten welcome." in rows[0]["content"]


def test_flow_template_copies_are_checked_lightly_and_settle():
    fake = account()
    _flow_with_its_own_copy(fake)
    state: dict = {}
    sync(fake, state)
    fake.calls.clear()
    rows, _ = sync(fake, state)
    assert rows == []
    checks = requested(fake, "/templates/FT1")
    assert checks == [{"fields[template]": "updated"}]  # one light request, no re-render
    assert requested(fake, "/flows/F2") == []


def test_a_deleted_flow_template_copy_re_renders_its_flow_once():
    fake = account()
    _flow_with_its_own_copy(fake)
    state: dict = {}
    sync(fake, state)
    del fake.templates["FT1"]
    rows, _ = sync(fake, state)
    assert by_id(rows)["flow:F2"]["content"].count("Original welcome.") == 0
    rows, _ = sync(fake, state)
    assert rows == []


def test_multi_line_step_bodies_stay_indented_under_their_step():
    fake = account()
    fake.edit_template("T1", html="<p>Line one.</p><p>Line two.</p>")
    flow = fake._flow("F1", {"additional-fields[flow]": "definition"})
    actions = _ordered_actions(flow["attributes"]["definition"])
    content = render_flow(flow, actions, {"T1": fake._template("T1", {})})["content"]
    assert "   Body:\n   Line one.\n   Line two.\n2. Wait 2 days" in content


def test_a_new_render_version_renders_every_record_once():
    fake = account()
    state: dict = {}
    sync(fake, state)
    assert state["render_version"] == kl._RENDER_VERSION
    state["render_version"] = kl._RENDER_VERSION - 1
    state["flows"]["known"]["F1"] = "an older rendering"
    rows, stats = sync(fake, state)
    assert set(by_id(rows)) == {"flow:F1"}  # the others render to the same text
    assert stats["unchanged"] == 5
    assert sync(fake, state)[0] == []


# ---------------------------------------------------------------------------
# Regressions from review
# ---------------------------------------------------------------------------
def test_records_that_come_into_scope_later_are_backfilled():
    fake = account()
    fake.add_campaign("CA", "Old archived promo", subject="Old", template_id="T2", archived=True)
    fake.add_campaign("C5", "Newer promo", subject="New", template_id="T2")  # moves the cursor on
    state: dict = {}
    rows, _ = sync(fake, state, resources=["campaigns"])
    assert "campaign:CA" not in by_id(rows)
    rows, _ = sync(fake, state, resources=["campaigns"], include_archived=True)
    assert "Archived: yes" in by_id(rows)["campaign:CA"]["content"]


def test_the_first_template_starting_point_is_a_single_request():
    fake = account()
    for n in range(25):
        fake.add_template(f"X{n}", f"Template {n}", html="<p>x</p>")
    sync(fake, {}, resources=["flows"])
    assert len(requested(fake, "/templates")) == 1


def test_an_edit_during_a_run_still_reaches_every_owner_of_the_template():
    fake = account()
    fake.add_campaign("C4", "Sent reminder", subject="Hi", template_id="T1")  # sent, uses T1
    state: dict = {}
    sync(fake, state, resources=["campaigns"])
    run = kl._Run(
        client(fake),
        state,
        kl._selected_kinds(["campaigns"]),
        kl._Options(),
        dict.fromkeys(kl._STAT_KEYS, 0),
    )
    run.list_all()
    fake.edit_template("T1", html="<p>Edited mid-run.</p>")  # C2 (draft) renders it now
    list(run.render())
    list(run.deletions())
    run.commit()
    rows, _ = sync(fake, state, resources=["campaigns"])
    assert "Edited mid-run." in by_id(rows)["campaign:C4"]["content"]


class _EditsAfterFirstListing(FakeKlaviyo):
    """Edits a template right after the first (filtered) templates listing of a run."""

    def __init__(self):
        super().__init__()
        self.edit_on_next_watch: tuple[str, str] | None = None

    def get(self, url, params=None, headers=None, timeout=None):
        response = super().get(url, params, headers, timeout)
        path, query = self.calls[-1]
        if path == "/templates" and "filter" in query and self.edit_on_next_watch:
            tid, html = self.edit_on_next_watch
            self.edit_on_next_watch = None
            self.edit_template(tid, html=html)
        return response


def test_a_template_edited_while_the_run_lists_is_not_lost():
    fake = _EditsAfterFirstListing()
    fake.add_template("T1", "Welcome", html="<p>Version one.</p>")
    state: dict = {}
    sync(fake, state, resources=["templates"])
    fake.edit_template("T1", html="<p>Version two.</p>")
    fake.edit_on_next_watch = ("T1", "<p>Version three.</p>")
    seen = [r["content"] for r in sync(fake, state, resources=["templates"])[0]]
    seen += [r["content"] for r in sync(fake, state, resources=["templates"])[0]]
    assert any("Version three." in content for content in seen)


class _SweepMisses(FakeKlaviyo):
    """A full flows listing (no filter) that leaves one flow out, once."""

    def __init__(self):
        super().__init__()
        self.miss: str | None = None

    def _list(self, collection, query):
        response = super()._list(collection, query)
        if collection == "flows" and "filter" not in query and self.miss:
            response._body["data"] = [r for r in response._body["data"] if r["id"] != self.miss]
            self.miss = None
        return response


def test_an_owner_missing_from_a_listing_still_gets_its_template_edit():
    fake = _SweepMisses()
    fake.add_template("FT1", "Welcome", html="<p>Original.</p>", listed=False)
    fake.add_flow("F2", "Browse", [email_step("b1", "Nudge", "Still looking?", "FT1")])
    state: dict = {}
    sync(fake, state, resources=["flows"])
    fake.edit_template("FT1", html="<p>Rewritten.</p>")
    fake.miss = "F2"
    seen = [r["content"] for r in sync(fake, state, resources=["flows"])[0]]
    seen += [r["content"] for r in sync(fake, state, resources=["flows"])[0]]
    assert any("Rewritten." in content for content in seen)
