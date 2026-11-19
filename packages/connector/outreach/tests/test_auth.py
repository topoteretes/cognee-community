"""OAuth tests: refresh on expiry and on 401, durable refresh-token rotation, token store,
and the ``outreach-auth`` authorization-code flow (loopback redirect, no network)."""

import contextlib
import json
import os
import socket
import stat
import threading
import time
import urllib.request

import pytest
from conftest import BASE_URL, FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, TOKEN_URL, FakeOutreach

from cognee_community_connector_outreach import OutreachAuthError, OutreachError
from cognee_community_connector_outreach import outreach as mod
from cognee_community_connector_outreach.outreach import TokenStore, _Api


@pytest.fixture
def api_for(session_for, make_auth):
    def build(fake, **auth_kwargs):
        return _Api(BASE_URL, make_auth(fake, **auth_kwargs), session_for(fake))

    return build


def stored(path):
    return json.loads(path.read_text())


# -- refresh on expiry -------------------------------------------------------------------


def test_token_is_refreshed_before_expiry(fake, api_for, seed_tokens, token_path):
    seed_tokens(expires_in=30)  # inside the 60s skew => proactively refreshed
    assert api_for(fake).probe("/sequences") == 200
    (grant,) = fake.token_calls
    assert grant["grant_type"] == "refresh_token" and grant["refresh_token"] == "rt-0"
    assert grant["client_id"] == FAKE_CLIENT_ID and grant["client_secret"] == FAKE_CLIENT_SECRET
    assert fake.calls[-1][2] == "Bearer at-1"
    assert stored(token_path)["access_token"] == "at-1"


def test_fresh_token_is_not_refreshed(fake, api_for):
    api = api_for(fake)
    for _ in range(3):
        api.probe("/sequences")
    assert fake.token_calls == []


def test_refresh_uses_injected_clock(fake, make_auth, seed_tokens):
    clock = [1_000.0]
    seed_tokens(expires_in=7200, clock=lambda: clock[0])
    auth = make_auth(fake, clock=lambda: clock[0])
    assert auth.access_token() == "at-0"
    clock[0] += 7200 - 59  # 59s left < 60s skew
    assert auth.access_token() == "at-1"
    assert auth.refresh_count == 1


# -- refresh once on 401 -------------------------------------------------------------------


def test_401_triggers_one_refresh_and_a_single_retry(fake, api_for, token_path):
    fake.valid_tokens.clear()  # the server revoked at-0 although it looks unexpired
    assert api_for(fake).probe("/sequences") == 200
    assert len(fake.token_calls) == 1
    assert [c[2] for c in fake.calls] == ["Bearer at-0", "Bearer at-1"]
    assert stored(token_path)["refresh_token"] == "rt-1"


def test_second_401_is_not_retried_again(fake, api_for):
    fake.status_for[("/sequences", "any")] = 401
    assert api_for(fake).probe("/sequences") == 401
    assert len(fake.token_calls) == 1 and fake.count("/sequences") == 2


# -- rotation ------------------------------------------------------------------------------


def test_rotated_refresh_token_is_persisted_before_the_new_access_token_is_used(
    fake, api_for, seed_tokens, monkeypatch
):
    seed_tokens(expires_in=0)
    events = []
    real_save = TokenStore.save

    def spy_save(self, tokens):
        events.append(("save", tokens["refresh_token"]))
        real_save(self, tokens)

    monkeypatch.setattr(TokenStore, "save", spy_save)
    real_send = FakeOutreach.send

    def spy_send(self, request, **kw):
        if request.headers.get("Authorization") == "Bearer at-1":
            events.append(("use", "at-1"))
        return real_send(self, request, **kw)

    monkeypatch.setattr(FakeOutreach, "send", spy_send)
    api_for(fake).probe("/sequences")
    assert events == [("save", "rt-1"), ("use", "at-1")]


def test_crash_right_after_rotation_loses_nothing(
    fake, make_auth, seed_tokens, token_path, monkeypatch
):
    seed_tokens(expires_in=0)
    real_save = TokenStore.save

    def save_then_crash(self, tokens):
        real_save(self, tokens)
        raise SystemExit("simulated crash after the rotated token was written")

    monkeypatch.setattr(TokenStore, "save", save_then_crash)
    with pytest.raises(SystemExit):
        make_auth(fake).access_token()
    monkeypatch.setattr(TokenStore, "save", real_save)

    # "Next process": the rotated pair is on disk, so no refresh with the dead rt-0 happens.
    assert stored(token_path)["refresh_token"] == "rt-1"
    fresh = make_auth(fake)
    assert fresh.access_token() == "at-1"
    assert len(fake.token_calls) == 1
    # And when that one expires, the *rotated* refresh token is the one presented.
    seed_tokens(access="at-1", refresh="rt-1", expires_in=0)
    make_auth(fake).access_token()
    assert fake.token_calls[-1]["refresh_token"] == "rt-1"


def test_failed_persist_means_the_new_token_is_never_used(fake, api_for, seed_tokens, monkeypatch):
    seed_tokens(expires_in=0)

    def boom(self, tokens):
        raise OSError("disk full")

    monkeypatch.setattr(TokenStore, "save", boom)
    with pytest.raises(Exception, match="disk full"):
        api_for(fake).probe("/sequences")
    assert all(c[2] != "Bearer at-1" for c in fake.calls)


def test_refresh_without_rotation_keeps_the_old_refresh_token(
    fake, api_for, seed_tokens, token_path
):
    fake.rotate = False
    seed_tokens(expires_in=0)
    api_for(fake).probe("/sequences")
    assert stored(token_path)["refresh_token"] == "rt-0"


def test_second_process_adopts_a_token_refreshed_by_the_first(fake, make_auth, seed_tokens):
    seed_tokens(expires_in=0)
    first, second = make_auth(fake), make_auth(fake)
    assert first.access_token() == "at-1"
    assert second.access_token() == "at-1"
    assert len(fake.token_calls) == 1  # no double refresh with a rotated-away refresh token


def test_permanent_refresh_failure_says_to_rerun_outreach_auth(fake, api_for, seed_tokens):
    seed_tokens(expires_in=0)
    fake.token_status = 400
    with pytest.raises(OutreachAuthError, match="outreach-auth"):
        api_for(fake).probe("/sequences")


def test_missing_token_store_says_to_run_outreach_auth(fake, make_auth, token_path):
    token_path.unlink()
    with pytest.raises(OutreachAuthError, match="outreach-auth"):
        make_auth(fake).access_token()


def test_token_endpoint_outage_is_not_reported_as_bad_credentials(fake, make_auth, seed_tokens):
    seed_tokens(expires_in=0)
    fake.token_status = 503
    with pytest.raises(OutreachError) as info:
        make_auth(fake).access_token()
    assert not isinstance(info.value, OutreachAuthError)


# -- token store ---------------------------------------------------------------------------


def test_token_store_is_private_and_atomic(token_path):
    store = TokenStore(token_path)
    store.save({"access_token": "a", "refresh_token": "r", "expires_at": 1})
    assert stat.S_IMODE(os.stat(token_path).st_mode) == 0o600
    store.save({"access_token": "b", "refresh_token": "r2", "expires_at": 2})
    assert store.load()["access_token"] == "b"
    assert [p.name for p in token_path.parent.iterdir() if p.suffix != ".lock"] == [token_path.name]


def test_failed_write_leaves_the_old_tokens_intact(token_path, monkeypatch):
    store = TokenStore(token_path)
    store.save({"access_token": "a", "refresh_token": "r", "expires_at": 1})
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        store.save({"access_token": "b", "refresh_token": "r2", "expires_at": 2})
    monkeypatch.undo()
    assert store.load()["access_token"] == "a"
    assert [p.name for p in token_path.parent.iterdir() if p.suffix != ".lock"] == [token_path.name]


def test_corrupt_token_store_is_reported(token_path):
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("{not json")
    with pytest.raises(OutreachAuthError, match="outreach-auth"):
        TokenStore(token_path).load()


def test_token_store_path_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTREACH_TOKEN_STORE", str(tmp_path / "x.json"))
    assert TokenStore().path == tmp_path / "x.json"


# -- outreach-auth ---------------------------------------------------------------------------


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hit(url, delay=0.3):
    def go():
        time.sleep(delay)
        with contextlib.suppress(Exception):  # a bad state is answered with a 400 => raises
            urllib.request.urlopen(url, timeout=5).read()

    thread = threading.Thread(target=go)
    thread.start()
    return thread


def test_authorize_url_has_required_parameters():
    url = mod.build_authorize_url("cid", "http://localhost:1/cb", ["a.read", "b.read"], "st")
    assert url.startswith(mod.AUTHORIZE_URL + "?")
    assert "client_id=cid" in url and "response_type=code" in url
    assert "scope=a.read+b.read" in url and "state=st" in url


def test_wait_for_code_checks_state():
    port = free_port()
    redirect = f"http://localhost:{port}/callback"
    thread = hit(f"{redirect}?code=the-code&state=good")
    assert mod.wait_for_code(redirect, "good", timeout=5) == "the-code"
    thread.join()

    thread = hit(f"{redirect}?code=the-code&state=forged")
    with pytest.raises(OutreachAuthError, match="invalid_state"):
        mod.wait_for_code(redirect, "good", timeout=5)
    thread.join()


def test_redirect_must_be_loopback():
    with pytest.raises(OutreachError, match="loopback"):
        mod.wait_for_code("http://evil.example/cb", "s")


def test_exchange_code_stores_tokens_0600(fake, session_for, tmp_path):
    store = TokenStore(tmp_path / "t.json")
    tokens = mod.exchange_code(
        "abc",
        client_id=FAKE_CLIENT_ID,
        client_secret=FAKE_CLIENT_SECRET,
        redirect_uri="http://localhost:1/cb",
        store=store,
        session=session_for(fake),
        token_url=TOKEN_URL,
    )
    assert fake.token_calls[0]["grant_type"] == "authorization_code"
    assert fake.token_calls[0]["code"] == "abc"
    assert store.load()["refresh_token"] == tokens["refresh_token"] == "rt-1"
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_exchange_code_requires_a_refresh_token(fake, session_for, tmp_path):
    fake.rotate = False
    with pytest.raises(OutreachError, match="refresh token"):
        mod.exchange_code(
            "abc",
            client_id="c",
            client_secret="s",
            redirect_uri="http://localhost:1/cb",
            store=TokenStore(tmp_path / "t.json"),
            session=session_for(fake),
        )


def test_outreach_auth_console_flow(fake, session_for, tmp_path, monkeypatch, capsys):
    port = free_port()
    monkeypatch.setattr(mod, "_build_token_session", lambda: session_for(fake))
    store_path = tmp_path / "cli" / "tokens.json"

    real_wait = mod.wait_for_code

    def wait_and_redirect(redirect_uri, state, timeout=300.0):
        # The state is random: play the browser by hitting the redirect with it.
        thread = hit(f"{redirect_uri}?code=cli-code&state={state}")
        try:
            return real_wait(redirect_uri, state, timeout=5)
        finally:
            thread.join()

    monkeypatch.setattr(mod, "wait_for_code", wait_and_redirect)
    code = mod.auth_main(
        [
            "--client-id",
            FAKE_CLIENT_ID,
            "--client-secret",
            FAKE_CLIENT_SECRET,
            "--redirect-uri",
            f"http://localhost:{port}/callback",
            "--token-store",
            str(store_path),
            "--no-browser",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and str(store_path) in out
    for secret in (FAKE_CLIENT_SECRET, "at-1", "rt-1"):
        assert secret not in out
    assert json.loads(store_path.read_text())["refresh_token"] == "rt-1"
    assert stat.S_IMODE(os.stat(store_path).st_mode) == 0o600


def test_outreach_auth_requires_credentials(monkeypatch):
    monkeypatch.delenv("OUTREACH_CLIENT_ID", raising=False)
    monkeypatch.delenv("OUTREACH_CLIENT_SECRET", raising=False)
    with pytest.raises(SystemExit):
        mod.auth_main(["--no-browser"])
