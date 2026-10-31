"""Unit and dlt-pipeline tests for the Dropbox Sign connector. No live account needed.

* Client tests run ``DropboxSignClient`` against ``httpx.MockTransport``.
* Rendering tests cover the request document and the no-form-data rule.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state, and
  ``dropbox_sign_source`` runs through a dlt pipeline into a temp sqlite destination.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import (
    DAY,
    NOW,
    SECRET_FIELD,
    SECRET_FORM,
    SECRET_META,
    SECRET_PHONE,
    SECRET_URL,
    FakeDropboxSign,
)

from cognee_community_connector_dropbox_sign import dropbox_sign as module
from cognee_community_connector_dropbox_sign.dropbox_sign import (
    DropboxSignAPIError,
    DropboxSignAuthError,
    DropboxSignClient,
    DropboxSignRateLimitedError,
    _DropboxSignConfig,
    _iter_rows,
    dropbox_sign_source,
    render_request,
)

NOW_DT = datetime.fromtimestamp(NOW, tz=timezone.utc)
KEY = "0123456789abcdef0123456789abcdef"


def _run(sign, state, stats=None, now=NOW_DT, check_active=None, **overrides):
    stats = stats if stats is not None else {}
    config = _DropboxSignConfig(**overrides)
    return list(_iter_rows(sign, config, state, stats, check_active, now=now))


def _live(rows):
    return sorted(row["id"] for row in rows if not row.get("_deleted"))


def _gone(rows):
    return sorted(row["id"] for row in rows if row.get("_deleted"))


def _doc(request_id):
    return f"signature_request:{request_id}"


# ---------------------------------------------------------------------------
# DropboxSignClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _client(responses, now=1000.0, **kwargs):
    queue = list(responses)
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = DropboxSignClient(
        KEY,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        clock=lambda: now,
        **kwargs,
    )
    return client, requests, sleeps


def test_client_sends_the_key_as_basic_auth_user():
    client, requests, _ = _client([httpx.Response(200, json={"account": {}})])
    assert client.get("/account", {"page": 1}) == {"account": {}}
    request = requests[0]
    assert str(request.url) == "https://api.hellosign.com/v3/account?page=1"
    expected = base64.b64encode(f"{KEY}:".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert KEY not in repr(client)


def test_client_waits_for_the_rate_limit_reset_and_retries_5xx():
    client, requests, sleeps = _client(
        [
            httpx.Response(429, headers={"X-Ratelimit-Reset": "1003"}),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert client.get("/account") == {"ok": True}
    assert len(requests) == 3
    assert sum(sleeps) == 3.0 + 2.0  # until the reset, then the second backoff step


def test_client_stops_on_a_long_reset_or_an_endless_429():
    client, _, sleeps = _client([httpx.Response(429, headers={"X-Ratelimit-Reset": "5000"})])
    with pytest.raises(DropboxSignRateLimitedError):
        client.get("/account")
    assert sleeps == []
    client, requests, _ = _client([httpx.Response(429)])
    with pytest.raises(DropboxSignRateLimitedError):
        client.get("/account")
    assert len(requests) == 5


def test_client_wait_budget_stops_retries_until_reset():
    client, _, _ = _client([httpx.Response(503)], max_sleep=2.0)
    with pytest.raises(DropboxSignRateLimitedError, match="wait budget"):
        client.get("/account")
    client.reset_sleep_budget()
    assert client._slept == 0.0


def test_client_errors_carry_the_status_only():
    body = {"error": {"error_msg": f"bad {KEY}", "error_name": "not_found"}}
    client, _, _ = _client([httpx.Response(410, json=body)])
    with pytest.raises(DropboxSignAPIError) as info:
        client.get("/signature_request/sr1")
    assert info.value.status == 410 and KEY not in str(info.value)
    client, _, _ = _client([httpx.Response(401, json=body)])
    with pytest.raises(DropboxSignAuthError) as auth:
        client.get("/account")
    assert KEY not in str(auth.value)


@pytest.mark.parametrize("key", ["", "   ", "key with space", "kéy", "key\tpart"])
def test_client_rejects_bad_keys_without_echoing_them(key):
    with pytest.raises(ValueError) as info:
        DropboxSignClient(key)
    assert key.strip() == "" or key.strip() not in str(info.value)


def test_client_waits_in_short_steps_asking_check_active_between():
    checks = []
    client, _, sleeps = _client(
        [httpx.Response(429, headers={"X-Ratelimit-Reset": "1003"}), httpx.Response(200, json={})],
        check_active=lambda: checks.append(1),
    )
    client.get("/account")
    assert sleeps == [1.0, 1.0, 1.0] and len(checks) == 4


def test_client_closes_only_the_http_client_it_made():
    injected = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    client = DropboxSignClient(KEY, http=injected)
    client.get("/account")
    client.close()
    assert not injected.is_closed
    owned = DropboxSignClient(KEY)
    owned._http = httpx.Client()
    made = owned._http
    owned.close()
    assert made.is_closed and owned._http is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_request_has_title_status_dates_and_signers(sign):
    request_id = sign.add(
        "Vendor renewal",
        signers=[("Priya Shah", "priya@example.com"), ("Sam Lee", "sam@example.com")],
        message="Renewal for 2027.",
    )
    sign.sign(request_id, 1)
    request = sign.requests[request_id]
    request["signatures"][0]["last_viewed_at"] = NOW - 120
    row = render_request(request, NOW_DT)
    assert row["id"] == _doc(request_id)
    assert row["title"] == "Signature request: Vendor renewal"
    assert row["url"] == f"https://app.hellosign.com/home/manage?guid={request_id}"
    content = row["content"]
    assert "Status: awaiting signatures" in content
    assert "Subject: Please sign: Vendor renewal" in content
    assert "- Priya Shah: awaiting signature, last viewed 2026-10-26" in content
    assert "- Sam Lee: signed on 2026-10-26" in content
    assert content.endswith("Renewal for 2027.")


def test_render_request_orders_signers_and_names_unnamed_ones(sign):
    request_id = sign.add(signers=[("B", "b@example.com"), ("", "a@example.com")])
    request = sign.requests[request_id]
    request["signatures"][0]["order"] = 1
    request["signatures"][1].update(order=0, signer_role="Client")
    content = render_request(request, NOW_DT)["content"]
    assert content.index("- Signer 1 (Client)") < content.index("- B:")
    assert "a@example.com" not in content


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"is_complete": True}, "complete"),
        ({"is_declined": True}, "declined"),
        ({"has_error": True}, "error"),
        ({"expires_at": NOW - 1}, "expired"),
        ({"expires_at": NOW + DAY}, "awaiting signatures"),
    ],
)
def test_render_request_status(sign, change, status):
    request = sign.requests[sign.add()]
    request.update(change)
    assert f"Status: {status}\n" in render_request(request, NOW_DT)["content"]


def test_emails_only_with_include_emails(sign):
    request = sign.requests[sign.add()]
    plain = render_request(request, NOW_DT)
    assert "@example.com" not in json.dumps(plain)
    with_emails = render_request(request, NOW_DT, include_emails=True)["content"]
    assert "- Priya Shah <priya@example.com>" in with_emails
    assert "Requested by: sender@example.com" in with_emails


def test_form_values_fields_urls_and_phones_never_reach_a_document(sign):
    request_id = sign.add(signers=[("Priya Shah", "priya@example.com")], message="See terms.")
    sign.decline(request_id)
    rows = _run(sign, {}, include_emails=True)
    text = json.dumps(rows)
    for secret in (SECRET_FORM, SECRET_FIELD, SECRET_META, SECRET_PHONE, SECRET_URL):
        assert secret not in text
    assert "cc-person@example.com" not in text and "files" not in text
    assert "declined, reason: Wrong rate" in text


def test_secrets_in_messages_are_scrubbed(sign):
    sign.add(message="Use key AKIAABCDEFGHIJKLMNOP to open the portal")
    stats = {}
    rows = _run(sign, {}, stats=stats)
    assert "AKIAABCDEFGHIJKLMNOP" not in rows[0]["content"]
    assert "[REDACTED:aws_access_key]" in rows[0]["content"] and stats["redacted"] == 1


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def test_first_sync_emits_every_request_and_no_change_emits_nothing(sign):
    a, b = sign.add("NDA"), sign.add("Offer letter")
    state, stats = {}, {}
    assert _live(_run(sign, state, stats=stats)) == sorted([_doc(a), _doc(b)])
    assert stats["emitted"] == 2 and stats["scanned"] == 2
    sign.calls.clear()
    stats = {}
    assert _run(sign, state, stats=stats) == []
    assert sign.paths() == ["/account", "/signature_request/list"]
    assert stats["unchanged"] == 2 and stats["emitted"] == 0


def test_a_signer_signing_emits_the_request_again(sign):
    a, b = sign.add("NDA"), sign.add("Offer letter")
    state = {}
    _run(sign, state)
    sign.sign(a)
    rows = _run(sign, state)
    assert _live(rows) == [_doc(a)] and "Status: complete" in rows[0]["content"]
    sign.decline(b)
    assert _live(_run(sign, state)) == [_doc(b)]


def test_a_canceled_request_is_forgotten(sign):
    a, b = sign.add("NDA"), sign.add("Offer letter")
    state, stats = {}, {}
    _run(sign, state)
    sign.cancel(a)
    rows = _run(sign, state, stats=stats)
    assert _gone(rows) == [_doc(a)] and _live(rows) == [] and stats["deleted"] == 1
    assert a not in state["requests"] and b in state["requests"]


def test_an_empty_listing_waits_for_confirmation(sign):
    a = sign.add()
    state = {}
    _run(sign, state)
    sign.cancel(a)
    assert _run(sign, state) == [] and state["empty_listing"] is True
    assert _gone(_run(sign, state)) == [_doc(a)] and "empty_listing" not in state


def test_a_listing_without_its_list_raises_and_forgets_nothing(sign):
    sign.add()
    state = {}
    _run(sign, state)
    sign.missing_list = True
    with pytest.raises(DropboxSignAPIError, match="without a request list"):
        _run(sign, state)
    assert len(state["requests"]) == 1


def test_a_listed_request_without_its_id_raises(sign):
    sign.add()
    sign.requests["sr0001"]["signature_request_id"] = ""
    with pytest.raises(DropboxSignAPIError, match="without its id"):
        _run(sign, {})


def test_the_listing_pages_through_every_page(sign):
    sign.page_size = 2
    ids = [sign.add(f"Doc {n}") for n in range(5)]
    rows = _run(sign, {})
    assert _live(rows) == sorted(_doc(i) for i in ids)
    pages = [params["page"] for path, params in sign.calls if path.endswith("/list")]
    assert pages == [1, 2, 3]


def test_requests_older_than_since_days_are_skipped(sign):
    old = [sign.add(f"Old {n}", created=NOW - 400 * DAY + n) for n in range(4)]
    recent = [sign.add(f"New {n}") for n in range(2)]
    rows = _run(sign, {})
    assert _live(rows) == sorted(_doc(i) for i in recent)
    assert not any(_doc(i) in str(rows) for i in old)


def test_every_page_is_read_whatever_the_order(sign):
    sign.page_size = 2
    sign.unordered = True
    [sign.add(f"Old {n}", created=NOW - 400 * DAY + n) for n in range(4)]
    recent = sign.add("New")
    assert _live(_run(sign, {})) == [_doc(recent)]
    pages = [params["page"] for path, params in sign.calls if path.endswith("/list")]
    assert pages == [1, 2, 3]


@pytest.mark.parametrize("unordered", [False, True])
def test_a_request_that_ages_out_of_the_window_is_kept(sign, unordered):
    sign.page_size = 1
    sign.unordered = unordered
    a = sign.add("NDA", created=NOW - 300 * DAY)
    sign.add("Offer letter")
    state = {}
    _run(sign, state)
    later = NOW_DT + timedelta(days=100)
    rows = _run(sign, state, now=later)
    assert _gone(rows) == [] and a in state["requests"]


def test_test_mode_requests_are_left_out_unless_asked_for(sign):
    real, test = sign.add("NDA"), sign.add("Trial", test_mode=True)
    stats = {}
    assert _live(_run(sign, {}, stats=stats)) == [_doc(real)]
    assert stats["skipped_test_mode"] == 1
    rows = _run(sign, {}, include_test_mode=True)
    assert _live(rows) == sorted([_doc(real), _doc(test)])
    assert "Test mode: yes" in next(r for r in rows if r["id"] == _doc(test))["content"]


def test_query_and_account_id_reach_the_listing(sign):
    nda, offer = sign.add("NDA"), sign.add("Offer letter")
    state = {}
    rows = _run(sign, state, query="title:NDA", account_id="all")
    assert _live(rows) == [_doc(nda)]
    params = next(p for path, p in sign.calls if path.endswith("/list"))
    assert params["query"] == "title:NDA" and params["account_id"] == "all"
    assert params["page_size"] == 100
    assert _doc(offer) not in str(rows)


def test_another_account_under_the_same_state_raises(sign):
    sign.add()
    state = {}
    _run(sign, state)
    other = FakeDropboxSign(account_id="acct-2")
    with pytest.raises(ValueError, match="another Dropbox Sign account"):
        _run(other, state)
    assert len(state["requests"]) == 1


def test_full_resync_emits_every_request_again(sign):
    a = sign.add()
    state = {}
    _run(sign, state)
    assert _live(_run(sign, state, full_resync=True)) == [_doc(a)]


def test_budget_stops_after_the_first_page_with_failed_set(sign):
    sign.page_size = 1
    a = sign.add("NDA")
    sign.add("Offer letter")
    sign.add("Lease")
    state = {}
    _run(sign, state)
    sign.cancel(a)
    stats = {}
    rows = _run(sign, state, stats=stats, max_requests=2)
    assert stats["failed"] == 1 and stats["failed_budget"] == 1
    assert _gone(rows) == [], "an incomplete listing forgets nothing"
    assert set(module._STATS) <= set(stats)


def test_a_rate_limit_stops_cleanly_and_keeps_what_was_emitted(sign):
    sign.add()
    sign.rate_limited.add("/signature_request/list")
    stats = {}
    assert _run(sign, {}, stats=stats) == []
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1


def test_a_key_revoked_mid_run_stops_cleanly_but_a_bad_key_raises(sign):
    sign.page_size = 1
    sign.add("NDA")
    sign.add("Offer letter")
    original = sign.get

    def revoke_after_first_page(path, params=None):
        if path.endswith("/list") and int((params or {}).get("page", 1)) > 1:
            raise DropboxSignAuthError("Dropbox Sign rejected the API key (HTTP 401)")
        return original(path, params)

    sign.get = revoke_after_first_page
    stats = {}
    rows = _run(sign, {}, stats=stats)
    assert len(rows) == 1 and stats["failed_auth"] == 1
    sign.get = original
    sign.bad_key = True
    with pytest.raises(DropboxSignAuthError):
        _run(sign, {})


def test_check_active_runs_before_every_request(sign):
    sign.page_size = 1
    sign.add("NDA")
    sign.add("Offer letter")
    checks = []
    _run(sign, {}, check_active=lambda: checks.append(len(sign.calls)))
    assert checks == [0, 1, 2]


def test_other_errors_raise_and_keep_the_previous_state(sign):
    sign.add()
    state = {}
    _run(sign, state)
    before = json.dumps(state, sort_keys=True)
    sign.errors["/signature_request/list"] = 500
    with pytest.raises(DropboxSignAPIError):
        _run(sign, state)
    assert json.dumps(state, sort_keys=True) == before


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(sign):
    from cognee.tasks.ingestion import dlt_utils

    source = dropbox_sign_source(service=sign)
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "dropbox_sign"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "dropbox_sign"
    assert source.name == "dropbox_sign"


def test_default_resource_name_follows_the_selection(sign):
    base = dropbox_sign_source(service=sign).name
    narrowed = dropbox_sign_source(service=sign, query="title:NDA").name
    team = dropbox_sign_source(service=sign, account_id="all").name
    assert base == "dropbox_sign"
    assert len({base, narrowed, team}) == 3 and narrowed.startswith("dropbox_sign_")


@pytest.mark.parametrize("name", ["", "Has-Caps", "a" * 65, "with space", 3])
def test_invalid_resource_name_is_rejected(sign, name):
    with pytest.raises(ValueError, match="resource_name"):
        dropbox_sign_source(service=sign, resource_name=name)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"since_days": 0},
        {"since_days": True},
        {"max_requests": 0},
        {"account_id": " "},
        {"query": ""},
    ],
)
def test_settings_are_validated(sign, kwargs):
    with pytest.raises(ValueError):
        dropbox_sign_source(service=sign, **kwargs)


def test_an_api_key_is_required_and_never_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DROPBOX_SIGN_API_KEY", KEY)
    with pytest.raises(ValueError, match="API key is required"):
        dropbox_sign_source()


def test_client_is_built_with_the_check_active_hook(monkeypatch):
    seen = {}

    def build(api_key, api_url, **kwargs):
        seen.update(api_key=api_key, api_url=api_url, **kwargs)
        return "c"

    monkeypatch.setattr(module, "DropboxSignClient", build)
    dropbox_sign_source(api_key=KEY)
    assert seen == {"api_key": KEY, "api_url": module.DEFAULT_API_URL, "check_active": None}


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "dropbox_sign.db").as_posix()
    return dlt.pipeline(
        pipeline_name="dropbox_sign_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="dropbox_sign_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline, table="dropbox_sign"):
    with pipeline.sql_client() as client, client.execute_query(f"SELECT id FROM {table}") as cursor:
        return {row[0] for row in cursor.fetchall()}


def test_pipeline_syncs_incrementally_and_forgets_a_canceled_request(
    dlt_mod, tmp_path, sign, monkeypatch
):
    monkeypatch.setattr(module, "_utcnow", lambda: NOW_DT)
    a, b = sign.add("NDA"), sign.add("Offer letter")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(dropbox_sign_source(service=sign))
    assert _staged(pipeline) == {_doc(a), _doc(b)}

    # The state lives in dlt, so a fresh source object resumes from it.
    source = dropbox_sign_source(service=sign)
    pipeline.run(source)
    assert source.cognee_sync_stats["emitted"] == 0

    sign.cancel(b)
    pipeline.run(dropbox_sign_source(service=sign))
    assert _staged(pipeline) == {_doc(a)}


def test_owned_client_is_closed_and_stats_carry_every_key(dlt_mod, tmp_path, sign, monkeypatch):
    closed = []

    class Owned:
        def __init__(self, api_key, api_url, **kwargs):
            pass

        def get(self, path, params=None):
            return sign.get(path, params)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "DropboxSignClient", Owned)
    sign.add()
    source = dropbox_sign_source(api_key=KEY)
    _pipeline(dlt_mod, tmp_path).run(source)
    assert closed == [True]
    assert set(module._STATS) <= set(source.cognee_sync_stats)
    assert source.cognee_sync_stats["failed"] == 0
