"""Offline OAuth acceptance tests with real RSA verification and shared file locks."""

import base64
import hashlib
import json
import multiprocessing
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cognee_community_connector_read_ai import auth, cli
from cognee_community_connector_read_ai.auth import (
    ISSUER,
    JWKS_URL,
    TOKEN_URL,
    USERINFO_URL,
    FileTokenProvider,
    OAuthIdentity,
    TokenBundle,
    exchange_code,
    register_client,
    save_credentials,
    verify_identity,
)
from cognee_community_connector_read_ai.models import AccountMismatchError, AuthenticationError


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token_bundle(private_key, **claims_override):
    claims = {
        "iss": ISSUER,
        "sub": "account-1",
        "aud": "client-1",
        "iat": int(time.time()) - 1,
        "exp": int(time.time()) + 300,
    }
    claims.update(claims_override)
    return TokenBundle(
        "access-secret",
        "client-1",
        jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"}),
    )


def identity_client(private_key, *, subject="account-1", status=200, jwk_override=None):
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update(kid="test-key", alg="RS256", use="sig")
    jwk.update(jwk_override or {})

    def handle(request):
        if str(request.url) == JWKS_URL:
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"keys": [jwk]})
        assert str(request.url) == USERINFO_URL
        assert request.headers["authorization"] == "Bearer access-secret"
        return httpx.Response(status, json={"sub": subject})

    return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)


def test_signed_identity_and_expected_userinfo(private_key):
    identity = OAuthIdentity(ISSUER, "account-1")
    with identity_client(private_key) as client:
        assert verify_identity(token_bundle(private_key), client=client) == identity
        assert (
            verify_identity(
                TokenBundle("access-secret", "client-1"), client=client, expected=identity
            )
            == identity
        )


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://attacker.example/"},
        {"aud": "other-client"},
        {"exp": int(time.time()) - 100},
        {"iat": int(time.time()) + 300},
        {"sub": ""},
        {"aud": ["client-1", "other"]},
        {"azp": "other"},
        {"at_hash": "incorrect"},
        {"iat": "123"},
        {"iat": float("inf")},
        {"exp": True},
    ],
)
def test_claim_validation(private_key, claims):
    with identity_client(private_key) as client, pytest.raises(AuthenticationError):
        verify_identity(token_bundle(private_key, **claims), client=client)


@pytest.mark.parametrize("required", ["iss", "sub", "aud", "iat", "exp"])
def test_required_claims(private_key, required):
    bundle = token_bundle(private_key)
    claims = jwt.decode(bundle.id_token, options={"verify_signature": False})
    del claims[required]
    broken = TokenBundle(
        bundle.access_token,
        bundle.client_id,
        jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"}),
    )
    with identity_client(private_key) as client, pytest.raises(AuthenticationError):
        verify_identity(broken, client=client)


def test_nonce_hash_and_multiple_audiences(private_key):
    access_hash = (
        base64.urlsafe_b64encode(hashlib.sha256(b"access-secret").digest()[:16])
        .rstrip(b"=")
        .decode()
    )
    signed = token_bundle(
        private_key,
        nonce="known-nonce",
        at_hash=access_hash,
        aud=["client-1", "other"],
        azp="client-1",
    )
    with identity_client(private_key) as client:
        assert (
            verify_identity(
                TokenBundle(signed.access_token, signed.client_id, signed.id_token, "known-nonce"),
                client=client,
            ).subject
            == "account-1"
        )
        with pytest.raises(AuthenticationError, match="nonce"):
            verify_identity(
                TokenBundle(signed.access_token, signed.client_id, signed.id_token, "wrong"),
                client=client,
            )


def test_reject_unsigned_initial_proof_and_changed_account(private_key):
    with identity_client(private_key) as client:
        with pytest.raises(AuthenticationError, match="ID token"):
            verify_identity(TokenBundle("access-secret", "client-1"), client=client)
        unsigned = TokenBundle(
            "access-secret", "client-1", jwt.encode({"sub": "account-1"}, "", algorithm="none")
        )
        with pytest.raises(AuthenticationError):
            verify_identity(unsigned, client=client)
        with pytest.raises(AccountMismatchError):
            verify_identity(
                token_bundle(private_key, sub="other"),
                client=client,
                expected=OAuthIdentity(ISSUER, "account-1"),
            )
    with (
        identity_client(private_key, subject="other") as client,
        pytest.raises(AccountMismatchError),
    ):
        verify_identity(token_bundle(private_key), client=client)


@pytest.mark.parametrize("key", [{"kid": "unknown"}, {"use": "enc"}, {"key_ops": ["sign"]}])
def test_signing_key_restrictions(private_key, key):
    with (
        identity_client(private_key, jwk_override=key) as client,
        pytest.raises(AuthenticationError),
    ):
        verify_identity(token_bundle(private_key), client=client)


def test_redirect_not_followed_and_no_secrets(private_key):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "https://attacker.example/?secret=access-secret"}
        )

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        with pytest.raises(AuthenticationError) as error:
            verify_identity(
                TokenBundle("access-secret", "client-1"),
                client=client,
                expected=OAuthIdentity(ISSUER, "account-1"),
            )
    assert calls == [USERINFO_URL]
    assert "access-secret" not in str(error.value)
    assert "access-secret" not in repr(token_bundle(private_key))


def credentials(tmp_path, **overrides):
    path = tmp_path / "credentials.json"
    values = {
        "client_id": "client-1",
        "client_secret": "client-secret",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "id_token": "old-proof",
        "expires_at": time.time() - 1,
        "nonce": "old-nonce",
    }
    values.update(overrides)
    save_credentials(path, values)
    return path


def token_response(**overrides):
    value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 599,
        "token_type": "bearer",
    }
    value.update(overrides)
    return httpx.Response(200, json=value)


def test_refresh_rotates_before_return_and_discards_old_idtoken(tmp_path):
    path = credentials(tmp_path)

    def handle(request):
        assert str(request.url) == TOKEN_URL
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"],
            "refresh_token": ["old-refresh"],
        }
        assert request.headers["authorization"].startswith("Basic ")
        return token_response()

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        bundle = FileTokenProvider(path, client=client).get_tokens()
    assert bundle.access_token == "new-access"
    assert bundle.id_token is None and bundle.nonce is None
    assert json.loads(path.read_text())["refresh_token"] == "new-refresh"
    assert path.stat().st_mode & 0o777 == 0o600
    assert Path(str(path) + ".lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["timeout", "500"])
def test_ambiguous_refresh_retries_immediately_once(tmp_path, failure):
    path = credentials(tmp_path)
    requests = []

    def handle(request):
        requests.append(request.content)
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("secret response access-secret", request=request)
            return httpx.Response(500)
        return token_response()

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert FileTokenProvider(path, client=client).get_tokens().access_token == "new-access"
    assert len(requests) == 2 and requests[0] == requests[1]


def test_permanent_refresh_failure_not_retried(tmp_path):
    path = credentials(tmp_path)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(400, json={"error_description": "refresh_token=secret"})

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(AuthenticationError) as error,
    ):
        FileTokenProvider(path, client=client).get_tokens()
    assert len(calls) == 1 and "secret" not in str(error.value)
    assert json.loads(path.read_text())["refresh_token"] == "old-refresh"


def test_refresh_failure_twice_requires_reauthorization(tmp_path):
    path = credentials(tmp_path)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(AuthenticationError, match="reconnect"),
    ):
        FileTokenProvider(path, client=client).get_tokens()
    assert len(calls) == 2


def test_persistence_failure_never_exposes_new_token(tmp_path, monkeypatch):
    path = credentials(tmp_path)

    def fail_replace(*args):
        raise OSError("sensitive filesystem diagnostics")

    monkeypatch.setattr(auth.os, "replace", fail_replace)
    with httpx.Client(transport=httpx.MockTransport(lambda request: token_response())) as client:
        with pytest.raises(AuthenticationError, match="durably save"):
            FileTokenProvider(path, client=client).get_tokens()
    assert json.loads(path.read_text())["access_token"] == "old-access"
    assert not list(tmp_path.glob(".credentials.json.*"))


def _refresh_process(path, count_path, output):
    def handle(request):
        count_path.write_text(count_path.read_text() + "request\n")
        time.sleep(0.1)
        return token_response()

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        output.put(FileTokenProvider(path, client=client).get_tokens().access_token)


def test_two_processes_refresh_only_once(tmp_path):
    path = credentials(tmp_path)
    counter = tmp_path / "counter"
    counter.write_text("")
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(target=_refresh_process, args=(path, counter, output)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert [output.get(timeout=2) for _ in processes] == ["new-access", "new-access"]
    assert counter.read_text() == "request\n"


def test_fresh_credentials_do_not_refresh(tmp_path):
    path = credentials(tmp_path, expires_at=time.time() + 300)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP"))
    ) as client:
        assert FileTokenProvider(path, client=client).get_tokens().id_token == "old-proof"


def test_insecure_or_symlink_credentials_rejected(tmp_path):
    path = credentials(tmp_path)
    path.chmod(0o644)
    with pytest.raises(AuthenticationError, match="0600"):
        FileTokenProvider(path).get_tokens()
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(AuthenticationError):
        FileTokenProvider(link).get_tokens()
    with pytest.raises(AuthenticationError):
        save_credentials(link, {})


def test_auth_response_size_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "MAX_AUTH_RESPONSE_BYTES", 100)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b" " * 101))
    ) as client:
        with pytest.raises(AuthenticationError, match="size limit"):
            verify_identity(
                TokenBundle("access-secret", "client-1"),
                client=client,
                expected=OAuthIdentity(ISSUER, "account-1"),
            )


def test_registration_and_code_exchange_contract():
    def handle(request):
        if str(request.url) == auth.REGISTER_URL:
            value = json.loads(request.content)
            assert value["redirect_uris"] == [auth.REDIRECT_URI]
            assert value["grant_types"] == ["authorization_code", "refresh_token"]
            assert value["response_types"] == ["code"]
            assert value["scope"] == "openid email offline_access profile meeting:read mcp:execute"
            assert value["token_endpoint_auth_method"] == "client_secret_basic"
            return httpx.Response(
                201, json={"client_id": "client-1", "client_secret": "client-secret"}
            )
        value = parse_qs(request.content.decode())
        assert value == {
            "grant_type": ["authorization_code"],
            "code": ["raw-code"],
            "code_verifier": ["raw-verifier"],
            "redirect_uri": [auth.REDIRECT_URI],
        }
        return token_response(id_token="proof")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        registration = register_client(client, "Cognee")
        values = exchange_code(
            client, **registration, code="raw-code", code_verifier="raw-verifier"
        )
    assert values["id_token"] == "proof"


def test_cli_preserves_registration_before_token_failure(tmp_path, monkeypatch, capsys):
    path = tmp_path / "credentials.json"

    def handle(request):
        if str(request.url) == auth.REGISTER_URL:
            return httpx.Response(
                200, json={"client_id": "client-1", "client_secret": "client-secret"}
            )
        assert json.loads(path.read_text())["client_secret"] == "client-secret"
        return httpx.Response(400, json={"error_description": "secret verifier"})

    real_client = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    inputs = iter(["raw-code", "raw-verifier", ""])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(inputs))
    assert cli.main(["--credentials", str(path)]) == 1
    captured = capsys.readouterr()
    assert "client-secret" not in captured.out + captured.err
    assert path.stat().st_mode & 0o777 == 0o600


def test_cli_rejects_git_checkout_before_registration(tmp_path):
    (tmp_path / ".git").mkdir()
    assert cli.main(["--credentials", str(tmp_path / "tokens.json")]) == 1
    assert not (tmp_path / "tokens.json").exists()


def test_cli_success_verifies_proof_and_hides_secrets(tmp_path, monkeypatch, capsys, private_key):
    path = tmp_path / "credentials.json"
    signed = token_bundle(private_key)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update(kid="test-key", alg="RS256", use="sig")

    def handle(request):
        if str(request.url) == auth.REGISTER_URL:
            return httpx.Response(
                200, json={"client_id": "client-1", "client_secret": "client-secret"}
            )
        if str(request.url) == TOKEN_URL:
            return token_response(access_token=signed.access_token, id_token=signed.id_token)
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json={"keys": [jwk]})
        assert str(request.url) == USERINFO_URL
        return httpx.Response(200, json={"sub": "account-1"})

    real_client = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    inputs = iter(["raw-code", "raw-verifier", ""])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(inputs))
    assert cli.main(["--credentials", str(path)]) == 0
    values = json.loads(path.read_text())
    assert values["identity"] == {"issuer": ISSUER, "subject": "account-1"}
    assert values["refresh_token"] == "new-refresh"
    captured = capsys.readouterr()
    assert f"credentials_path={str(path)!r}" in captured.out
    for secret in ("client-secret", "access-secret", "new-refresh", signed.id_token):
        assert secret not in captured.out + captured.err
