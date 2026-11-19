"""Unit tests for the Customer.io connector.

The App API is mocked via ``FakeSession`` — no network traffic and no live
credentials are required, so these run in CI. Response shapes (field names,
the HTML around a message, the footer link, empty-string default language,
the 500 caused by a half-built SMS step) are copied from a live trial
workspace.
"""

import pytest
import requests

from cognee_community_connector_customerio import customerio
from cognee_community_connector_customerio.customerio import (
    DEFAULT_MESSAGE_TYPES,
    DOCUMENT_SOURCE_ATTR,
    MESSAGES_TABLE_NAME,
    _Api,
    _html_to_text,
    _make_session,
    _message_to_row,
    _paginate,
    customerio_source,
    sync_messages,
)

US = "https://api.customer.io/v1"
EU = "https://api-eu.customer.io/v1"


# ---------------------------------------------------------------------------
# Fake Customer.io App API
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Answers GETs from a ``{path: response}`` routing table and records calls.

    A path maps to one response, or to a list of responses served in order
    (the last one repeats). Paged requests are routed as ``path?start=cursor``.
    """

    def __init__(self, routes, base=US):
        self.routes = dict(routes)
        self.base = base
        self.calls = []

    def get(self, url, params=None, timeout=None):
        assert url.startswith(self.base), url
        path = url[len(self.base) :]
        if params and params.get("start"):
            path = f"{path}?start={params['start']}"
        self.calls.append(path)
        response = self.routes.get(path, FakeResponse(404, {"errors": [{"status": "404"}]}))
        if isinstance(response, list):
            response = response.pop(0) if len(response) > 1 else response[0]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Pacing and retries sleep; make that instant in tests."""
    monkeypatch.setattr(customerio.time, "sleep", lambda _seconds: None)


def ok(body):
    return FakeResponse(200, body)


def _html(text):
    """An email body shaped like Customer.io's Design Studio output (trimmed)."""
    return (
        '<!doctype html><html lang="und" dir="auto"><head><meta charset="utf-8" />'
        "<title>{{message.subject | default: ''}}</title>"
        "<style>p { margin: 16px 0; }</style></head><body>"
        f'<div style="padding: 20px;"><p style="margin: 16px 0;">{text}</p></div>'
        '<div style="padding: 20px;"><p style="text-align: center;">'
        '<a href="{% unsubscribe_url %}" class="inline-link">Unsubscribe</a></p></div>'
        "</body></html>"
    )


def _email(action_id, name, subject, text, language="", **extra):
    return {
        "id": str(action_id),
        "type": "email",
        "name": name,
        "language": language,
        "subject": subject,
        "preheader_text": "",
        "body": _html(text),
        "body_plain": "",
        "from": "Test Delivery Mode <test@customeriotest.com>",
        "recipient": "{{customer.email}}",
        "bcc": "",
        "headers": "[]",
        "updated": 1792010816,
        "deduplicate_id": f"{action_id}:1792010816",
        **extra,
    }


def _campaign(campaign_id, name, actions):
    return {
        "id": campaign_id,
        "name": name,
        "state": "draft",
        "updated": 1792010816,
        "actions": [{"id": int(a["id"]), "type": a["type"]} for a in actions],
    }


WELCOME_ACTIONS = [
    _email(2, "Email 1", "hello", "Welcome aboard"),
    _email(
        3,
        "Welcome: Email 1 (fr)",
        "bonjour",
        "Bienvenue à bord",
        language="fr",
        multi_language_branch_action_id=4,
    ),
]
WELCOME = _campaign(1, "Welcome", WELCOME_ACTIONS)


def _newsletter(newsletter_id, name, content_ids, type_="email"):
    return {"id": newsletter_id, "name": name, "type": type_, "content_ids": content_ids}


def _content(content_id, newsletter_id, name, text, **extra):
    return {
        "id": content_id,
        "newsletter_id": newsletter_id,
        "type": "email",
        "name": name,
        "language": "",
        "subject": name,
        "body": _html(text),
        "test_group_id": 0,
        **extra,
    }


def _workspace(**overrides):
    """A small workspace: one campaign (en + fr email), one newsletter."""
    routes = {
        "/campaigns": ok({"campaigns": [WELCOME]}),
        "/campaigns/1/actions": ok({"actions": WELCOME_ACTIONS}),
        "/newsletters": ok({"newsletters": [_newsletter(1, "October news", [5])], "next": ""}),
        "/newsletters/1/contents": ok(
            {"contents": [_content(5, 1, "October news", "What shipped in October")]}
        ),
    }
    routes.update(overrides)
    return FakeSession(routes)


def _api(session):
    return _Api(session, US)


def _sync(session, **kwargs):
    return list(sync_messages(_api(session), **kwargs))


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def test_session_uses_bearer_app_api_key():
    session = _make_session("secret-key")
    assert session.headers["Authorization"] == "Bearer secret-key"
    assert session.headers["Accept"] == "application/json"


def test_get_retries_rate_limit_and_server_errors_then_succeeds():
    session = FakeSession(
        {"/campaigns": [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(503), ok({})]}
    )

    assert _api(session).get("/campaigns").status_code == 200
    assert len(session.calls) == 3


def test_get_retries_network_errors():
    session = FakeSession({"/campaigns": [requests.ConnectionError("reset"), ok({})]})

    assert _api(session).get("/campaigns").status_code == 200


def test_get_does_not_retry_permanent_errors():
    # A 401 (bad API key) won't fix itself, so it is returned immediately.
    session = FakeSession({"/campaigns": FakeResponse(401)})

    assert _api(session).get("/campaigns").status_code == 401
    assert len(session.calls) == 1


def test_requests_are_paced_to_ten_per_second(monkeypatch):
    sleeps = []
    clock = iter([0.0, 0.0, 0.02, 0.1])  # monotonic() before/after each request
    monkeypatch.setattr(customerio.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(customerio.time, "sleep", sleeps.append)
    api = _api(FakeSession({"/campaigns": ok({})}))

    api.get("/campaigns")
    api.get("/campaigns")

    assert sleeps == [pytest.approx(0.08)]


def test_paginate_follows_next_cursor():
    session = FakeSession(
        {
            "/newsletters": ok({"newsletters": [{"id": 1}], "next": "abc"}),
            "/newsletters?start=abc": ok({"newsletters": [{"id": 2}], "next": ""}),
        }
    )

    assert [n["id"] for n in _paginate(_api(session), "/newsletters", "newsletters")] == [1, 2]


def test_paginate_refuses_a_repeated_cursor():
    session = FakeSession(
        {
            "/newsletters": ok({"newsletters": [], "next": "abc"}),
            "/newsletters?start=abc": ok({"newsletters": [], "next": "abc"}),
        }
    )

    with pytest.raises(RuntimeError, match="same page cursor"):
        list(_paginate(_api(session), "/newsletters", "newsletters"))


# ---------------------------------------------------------------------------
# Messages → rows
# ---------------------------------------------------------------------------
def test_email_row_has_text_without_the_unsubscribe_footer():
    row = _message_to_row("action", WELCOME_ACTIONS[0], "Welcome", DEFAULT_MESSAGE_TYPES)

    assert row == {
        "id": "action:2",
        "title": "Welcome: Email 1",
        "content": (
            "Campaign: Welcome\nMessage: Email 1\nChannel: email\nLanguage: default\n"
            "Subject: hello\n\nWelcome aboard"
        ),
    }


def test_rows_leave_out_addresses_and_timestamps():
    # Only text goes in, so editing one language (which bumps ``updated`` on
    # every language version) doesn't change the other versions' rows.
    row = _message_to_row("action", WELCOME_ACTIONS[0], "Welcome", DEFAULT_MESSAGE_TYPES)

    assert set(row) == {"id", "title", "content"}
    for leaked in ("customeriotest.com", "customer.email", "1792010816"):
        assert leaked not in row["content"]


def test_unchanged_message_renders_the_same_after_a_publish():
    # Seen live: publishing the default language changed the French version's
    # ``updated``/``deduplicate_id`` and turned its None fields into "".
    before = dict(WELCOME_ACTIONS[1], bcc=None, preheader_text=None, recipient="{{customer.email}}")
    after = dict(
        WELCOME_ACTIONS[1],
        bcc="",
        preheader_text="",
        recipient="",
        updated=1792011428,
        deduplicate_id="3:1792011428",
    )

    assert _message_to_row("action", before, "Welcome", DEFAULT_MESSAGE_TYPES) == _message_to_row(
        "action", after, "Welcome", DEFAULT_MESSAGE_TYPES
    )


def test_translation_keeps_its_language_and_its_own_id():
    row = _message_to_row("action", WELCOME_ACTIONS[1], "Welcome", DEFAULT_MESSAGE_TYPES)

    assert row["id"] == "action:3"
    # Translation names already start with the campaign name; not repeated.
    assert row["title"] == "Welcome: Email 1 (fr)"
    assert "Language: fr" in row["content"]
    assert row["content"].endswith("Bienvenue à bord")


def test_action_and_newsletter_content_ids_never_collide():
    # Both were id 5 in the trial workspace: they're numbered separately.
    action = _message_to_row("action", _email(5, "Email", "s", "a"), "C", DEFAULT_MESSAGE_TYPES)
    content = _message_to_row(
        "newsletter_content", _content(5, 1, "N", "b"), "N", DEFAULT_MESSAGE_TYPES
    )

    assert action["id"] != content["id"]
    assert content["content"].startswith("Newsletter: N")


def test_push_uses_title_and_plain_body():
    push = {"id": "7", "type": "push", "name": "Push 1", "language": "", "subject": "Hi"}
    push["body"] = "Your order shipped"

    row = _message_to_row("action", push, "Orders", DEFAULT_MESSAGE_TYPES)

    assert "Channel: push\nLanguage: default\nTitle: Hi" in row["content"]
    assert row["content"].endswith("Your order shipped")


def test_unfinished_messages_are_skipped():
    # A push step whose app isn't connected yet comes back with no text at all.
    push = {"id": "7", "type": "push", "name": "Push 1", "subject": "", "body": ""}

    assert _message_to_row("action", push, "Orders", DEFAULT_MESSAGE_TYPES) is None


def test_in_app_html_drops_the_bridge_script():
    in_app = {
        "id": "9",
        "type": "in_app",
        "name": "In-App Message 1",
        "language": "",
        "body": "<html><head><script>document.addEventListener('click', f)</script>"
        "</head><body><div>New dashboard is live</div></body></html>",
    }

    row = _message_to_row("action", in_app, "Launch", DEFAULT_MESSAGE_TYPES)

    assert row["content"].endswith("Channel: in-app\nLanguage: default\n\nNew dashboard is live")


def test_sms_is_ingested_as_plain_text():
    sms = {"id": "8", "type": "twilio", "name": "SMS 1", "language": "", "body": "Code: 1234"}

    row = _message_to_row("action", sms, "OTP", DEFAULT_MESSAGE_TYPES)

    assert "Channel: sms" in row["content"]
    assert row["content"].endswith("Code: 1234")


def test_types_not_asked_for_are_skipped():
    slack = {"id": "10", "type": "slack", "name": "Notify team", "body": "New signup"}

    assert _message_to_row("action", slack, "Ops", DEFAULT_MESSAGE_TYPES) is None
    assert _message_to_row("action", slack, "Ops", (*DEFAULT_MESSAGE_TYPES, "slack"))


def test_html_to_text_keeps_blocks_on_their_own_lines():
    html = "<p>One</p><p>Two &amp; three</p><ul><li>a</li><li>b</li></ul>Line<br/>break"

    assert _html_to_text(html) == "One\nTwo & three\na\nb\nLine\nbreak"


def test_liquid_logic_is_dropped_but_placeholders_stay():
    html = "<p>{% if customer.vip %}Hi {{customer.first_name}}{% endif %}</p>"

    assert _html_to_text(html) == "Hi {{customer.first_name}}"


def test_preferences_link_is_dropped_too():
    html = '<p>Hello</p><a href="{% manage_subscription_preferences_url %}">Preferences</a>'

    assert _html_to_text(html) == "Hello"


# ---------------------------------------------------------------------------
# Syncing a workspace
# ---------------------------------------------------------------------------
def test_sync_yields_every_campaign_and_newsletter_message():
    rows = _sync(_workspace())

    assert [r["id"] for r in rows] == ["action:2", "action:3", "newsletter_content:5"]


def test_newsletters_can_be_switched_off():
    session = _workspace()

    rows = _sync(session, include_newsletters=False)

    assert [r["id"] for r in rows] == ["action:2", "action:3"]
    assert "/newsletters" not in session.calls


def test_webhook_newsletters_are_not_fetched():
    session = _workspace(
        **{"/newsletters": ok({"newsletters": [_newsletter(2, "Ping CRM", [6], "webhook")]})}
    )

    assert [r["id"] for r in _sync(session)] == ["action:2", "action:3"]
    assert "/newsletters/2/contents" not in session.calls


def test_broken_message_falls_back_to_fetching_each_one():
    # Seen live: a half-built SMS step (action 8) made the whole actions list
    # return 500, while every other action still loaded by id.
    sms_stub = {"id": "8", "type": "twilio"}
    campaign = _campaign(1, "Welcome", [*WELCOME_ACTIONS, sms_stub])
    session = _workspace(
        **{
            "/campaigns": ok({"campaigns": [campaign]}),
            "/campaigns/1/actions": FakeResponse(500),
            "/campaigns/1/actions/2": ok({"action": WELCOME_ACTIONS[0]}),
            "/campaigns/1/actions/3": ok({"action": WELCOME_ACTIONS[1]}),
            "/campaigns/1/actions/8": FakeResponse(500),
        }
    )

    rows = _sync(session)

    assert [r["id"] for r in rows] == ["action:2", "action:3", "newsletter_content:5"]


def test_run_aborts_when_no_message_can_be_fetched():
    # If every per-message fetch fails too, the API itself is down. Loading
    # now would forget the whole workspace, so the run must fail instead.
    session = _workspace(
        **{
            "/campaigns/1/actions": FakeResponse(500),
            "/campaigns/1/actions/2": FakeResponse(500),
            "/campaigns/1/actions/3": FakeResponse(500),
            "/newsletters/1/contents": FakeResponse(500),
            "/newsletters/1/contents/5": FakeResponse(500),
        }
    )

    with pytest.raises(RuntimeError, match="aborting so nothing is forgotten"):
        _sync(session)


def test_campaign_deleted_mid_run_is_just_gone():
    session = _workspace(**{"/campaigns/1/actions": FakeResponse(404)})

    assert [r["id"] for r in _sync(session)] == ["newsletter_content:5"]


def test_failed_campaign_listing_aborts_the_run():
    session = _workspace(**{"/campaigns": FakeResponse(503)})

    with pytest.raises(requests.HTTPError):
        _sync(session)


def test_auth_failure_aborts_instead_of_falling_back():
    session = _workspace(**{"/campaigns/1/actions": FakeResponse(401)})

    with pytest.raises(requests.HTTPError):
        _sync(session)


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------
def test_source_requires_an_api_key(monkeypatch):
    monkeypatch.delenv("CUSTOMERIO_APP_API_KEY", raising=False)

    with pytest.raises(ValueError, match="App API key required"):
        customerio_source()


def test_source_reads_key_and_region_from_env(monkeypatch):
    monkeypatch.setenv("CUSTOMERIO_APP_API_KEY", "env-key")
    monkeypatch.setenv("CUSTOMERIO_REGION", "EU")
    session = FakeSession({"/campaigns": ok({"campaigns": []})}, base=EU)
    used_keys = []

    def fake_make_session(api_key):
        used_keys.append(api_key)
        return session

    monkeypatch.setattr(customerio, "_make_session", fake_make_session)

    list(customerio_source(include_newsletters=False).resources[MESSAGES_TABLE_NAME])

    assert used_keys == ["env-key"]
    assert session.calls == ["/campaigns"]  # served from the EU host


def test_eu_region_uses_the_eu_host():
    session = FakeSession({"/campaigns": ok({"campaigns": []})}, base=EU)

    source = customerio_source(region="eu", include_newsletters=False, session=session)
    list(source.resources[MESSAGES_TABLE_NAME])

    assert session.calls == ["/campaigns"]


def test_source_rejects_unknown_region():
    with pytest.raises(ValueError, match="region"):
        customerio_source(region="apac", session=FakeSession({}))


def test_source_refuses_webhooks():
    with pytest.raises(ValueError, match="Webhook"):
        customerio_source(message_types=("email", "webhook"), session=FakeSession({}))


def test_source_needs_something_to_sync():
    with pytest.raises(ValueError, match="Nothing to sync"):
        customerio_source(
            include_campaigns=False, include_newsletters=False, session=FakeSession({})
        )


def test_source_is_a_full_snapshot_in_document_mode():
    source = customerio_source(session=_workspace())
    resource = source.resources[MESSAGES_TABLE_NAME]

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "customerio"
    assert resource.write_disposition == "replace"


def test_forget_on_delete_end_to_end_through_a_real_dlt_load(tmp_path):
    import dlt

    pipeline = dlt.pipeline(
        pipeline_name="test_customerio_e2e",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'customerio.db'}"),
        dataset_name="cio",
    )

    def stored():
        with pipeline.sql_client() as client:
            rows = client.execute_sql(f"SELECT id, content FROM {MESSAGES_TABLE_NAME} ORDER BY id")
        return {row[0]: row[1] for row in rows}

    # Sync #1: two campaign messages and one newsletter land in staging.
    pipeline.run(customerio_source(session=_workspace()))
    assert sorted(stored()) == ["action:2", "action:3", "newsletter_content:5"]

    # Sync #2: the newsletter was deleted (gone from the list) and the English
    # email was edited. The snapshot replaces staging: the newsletter is gone
    # (cognee's orphan_cleanup forgets it) and the edit is picked up.
    edited = dict(WELCOME_ACTIONS[0], body=_html("Welcome aboard, again"))
    session = _workspace(
        **{
            "/campaigns/1/actions": ok({"actions": [edited, WELCOME_ACTIONS[1]]}),
            "/newsletters": ok({"newsletters": [], "next": ""}),
        }
    )
    pipeline.run(customerio_source(session=session))

    rows = stored()
    assert sorted(rows) == ["action:2", "action:3"]
    assert rows["action:2"].endswith("Welcome aboard, again")
