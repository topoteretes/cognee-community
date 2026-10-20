"""Settings, key-pair login and the connection check.

No Snowflake account is needed: logins and queries go to fakes. The error
codes and texts are the ones a real trial account returned (connector 4.8).
"""

import logging

import pytest
import snowflake.connector
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fake_snowflake import FakeConnection
from snowflake.connector.errors import DatabaseError, ProgrammingError

import cognee_community_connector_snowflake.snowflake as module
from cognee_community_connector_snowflake.snowflake import (
    SnowflakeAuthError,
    SnowflakeConnectorError,
    SnowflakeWarehouseError,
    _connect,
    _load_private_key,
    _normalize_account,
    _query,
    _resolve_settings,
    check_connection,
)

ENV = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PRIVATE_KEY",
    "SNOWFLAKE_PRIVATE_KEY_PATH",
    "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
    "SNOWFLAKE_ROLE",
    "SNOWFLAKE_WAREHOUSE",
)
LOGIN = "someone@example.com"


@pytest.fixture(autouse=True)
def no_snowflake_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key, passphrase=None):
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )


@pytest.fixture
def key_file(tmp_path, rsa_key):
    path = tmp_path / "rsa_key.p8"
    path.write_bytes(pem(rsa_key))
    return path


def settings(rsa_key, **overrides):
    values = {"account": "ORG-ACCT", "user": LOGIN, "private_key": pem(rsa_key), **overrides}
    return _resolve_settings(**values)


def snowflake_error(cls, errno, msg):
    return cls(msg=msg, errno=errno, sqlstate="08001", send_telemetry=False)


# -- settings ----------------------------------------------------------------


def test_settings_come_from_the_environment(monkeypatch, key_file):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ORG-ACCT")
    monkeypatch.setenv("SNOWFLAKE_USER", LOGIN)
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setenv("SNOWFLAKE_ROLE", "ANALYST")
    monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "WH")

    resolved = _resolve_settings()

    assert (resolved.account, resolved.user, resolved.role, resolved.warehouse) == (
        "ORG-ACCT",
        LOGIN,
        "ANALYST",
        "WH",
    )
    assert resolved.key_fingerprint.startswith("SHA256:")


def test_missing_settings_are_all_named_before_any_login():
    with pytest.raises(ValueError) as excinfo:
        _resolve_settings()

    message = str(excinfo.value)
    for name in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY_PATH"):
        assert name in message


def test_the_key_never_shows_in_repr(rsa_key):
    resolved = settings(rsa_key)

    assert "private_key" not in repr(resolved)
    assert resolved.private_key.hex()[:40] not in repr(resolved)


def test_a_missing_key_file_is_a_clear_error(tmp_path):
    with pytest.raises(SnowflakeAuthError, match="could not be read"):
        _resolve_settings(
            account="ORG-ACCT", user=LOGIN, private_key_path=str(tmp_path / "nope.p8")
        )


@pytest.mark.parametrize(
    "typed",
    [
        "ORG-ACCT",
        "  ORG-ACCT  ",
        "https://ORG-ACCT.snowflakecomputing.com/",
        "ORG-ACCT.snowflakecomputing.com",
        "http://org-acct.SNOWFLAKECOMPUTING.com",
    ],
)
def test_account_identifier_can_be_pasted_from_the_browser(typed):
    assert _normalize_account(typed).lower() == "org-acct"


@pytest.mark.parametrize(
    "typed", ["", "   ", "ORG ACCT", "https://ORG-ACCT.snowflakecomputing.com/x"]
)
def test_a_broken_account_identifier_is_refused(typed):
    with pytest.raises(ValueError, match="ORGNAME-ACCOUNTNAME"):
        _normalize_account(typed)


# -- private key ---------------------------------------------------------------


def test_key_is_passed_as_der_with_the_fingerprint_snowflake_shows(rsa_key):
    der, fingerprint = _load_private_key(pem(rsa_key), None)

    assert der == rsa_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    # Same format as RSA_PUBLIC_KEY_FP in DESC USER: SHA256 of the public key, base64.
    assert fingerprint.startswith("SHA256:") and len(fingerprint) == len("SHA256:") + 44
    assert _load_private_key(pem(rsa_key).decode(), None)[1] == fingerprint


def test_an_encrypted_key_works_with_its_passphrase(rsa_key):
    der, _ = _load_private_key(pem(rsa_key, "s3cret-phrase"), "s3cret-phrase")

    assert der == _load_private_key(pem(rsa_key), None)[0]


def test_an_encrypted_key_without_passphrase_says_so(rsa_key):
    with pytest.raises(SnowflakeAuthError, match="SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
        _load_private_key(pem(rsa_key, "s3cret-phrase"), None)


def test_a_wrong_passphrase_is_refused_without_echoing_it(rsa_key):
    with pytest.raises(SnowflakeAuthError) as excinfo:
        _load_private_key(pem(rsa_key, "s3cret-phrase"), "wrong-phrase")

    assert "wrong-phrase" not in str(excinfo.value)
    assert "s3cret-phrase" not in str(excinfo.value)


def test_a_passphrase_for_a_plain_key_says_so(rsa_key):
    with pytest.raises(SnowflakeAuthError, match="not encrypted"):
        _load_private_key(pem(rsa_key), "unneeded")


def test_text_that_is_not_a_key_is_refused_without_echoing_it():
    with pytest.raises(SnowflakeAuthError, match="PEM") as excinfo:
        _load_private_key("not-a-key-at-all", None)

    assert "not-a-key-at-all" not in str(excinfo.value)


def test_a_non_rsa_key_is_refused():
    key = ec.generate_private_key(ec.SECP256R1())

    with pytest.raises(SnowflakeAuthError, match="RSA"):
        _load_private_key(pem(key), None)


# -- login ---------------------------------------------------------------------


def test_login_uses_key_pair_auth_with_the_der_key(rsa_key):
    seen = {}
    resolved = settings(rsa_key, role="ANALYST", warehouse="WH")

    _connect(resolved, connect=lambda **kwargs: seen.update(kwargs) or "session")

    assert seen["authenticator"] == "SNOWFLAKE_JWT"
    assert seen["private_key"] == resolved.private_key
    assert (seen["account"], seen["user"], seen["role"], seen["warehouse"]) == (
        "ORG-ACCT",
        LOGIN,
        "ANALYST",
        "WH",
    )


def failing_login(error):
    def connect(**kwargs):
        raise error

    return connect


def test_jwt_invalid_names_both_causes_and_the_fingerprint(rsa_key):
    resolved = settings(rsa_key)
    error = snowflake_error(DatabaseError, 390144, "Failed to login: JWT token is invalid.")

    with pytest.raises(SnowflakeAuthError) as excinfo:
        _connect(resolved, connect=failing_login(error))

    message = str(excinfo.value)
    assert "login name" in message and "LOGIN_NAME" in message
    assert resolved.key_fingerprint in message
    # The login name is often an email address; it never reaches the message.
    assert LOGIN not in message


@pytest.mark.parametrize(
    ("errno", "text", "expected"),
    [
        (390189, "Role 'X' specified in the connect string does not exist", "role is not granted"),
        (290404, "404 Not Found: post ORG-NOPE.snowflakecomputing.com:443", "no account"),
        (250001, "Failed to login: 404 Not Found, message: Unexpected response", "no account"),
    ],
)
def test_login_errors_get_the_usual_fix(rsa_key, errno, text, expected):
    error = snowflake_error(DatabaseError, errno, text)

    with pytest.raises(SnowflakeAuthError, match=expected):
        _connect(settings(rsa_key), connect=failing_login(error))


def test_an_unknown_login_error_keeps_its_code(rsa_key):
    error = snowflake_error(DatabaseError, 123456, "Something else went wrong\nmore detail")

    with pytest.raises(
        SnowflakeConnectorError,
        match=r"^Snowflake error 123456 \(08001\): Something else went wrong$",
    ):
        _connect(settings(rsa_key), connect=failing_login(error))


def test_the_driver_banner_is_quieted_only_when_nobody_configured_it(monkeypatch, rsa_key):
    driver = logging.getLogger("snowflake.connector")
    monkeypatch.setattr(driver, "level", logging.NOTSET)
    monkeypatch.setattr(snowflake.connector, "connect", lambda **kwargs: "session")

    _connect(settings(rsa_key))
    assert driver.level == logging.WARNING

    driver.setLevel(logging.DEBUG)
    _connect(settings(rsa_key))
    assert driver.level == logging.DEBUG


# -- queries and the connection check ------------------------------------------


def session(warehouse="WH", state="STARTED", auto_resume="true", visible=True):
    def answers(sql):
        if sql.startswith("SELECT CURRENT_USER()"):
            return [{"U": "someone", "R": "ANALYST", "W": warehouse}]
        if sql.startswith("SHOW WAREHOUSES"):
            # LIKE is a pattern, so a similar name can come back too.
            rows = [{"name": "WH_OTHER", "state": "STARTED", "auto_resume": "true"}]
            if visible:
                rows.append({"name": warehouse, "state": state, "auto_resume": auto_resume})
            return rows
        raise AssertionError(sql)

    return FakeConnection(answers)


@pytest.fixture
def env_settings(monkeypatch, key_file):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ORG-ACCT")
    monkeypatch.setenv("SNOWFLAKE_USER", LOGIN)
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", str(key_file))


def use_session(monkeypatch, conn):
    monkeypatch.setattr(snowflake.connector, "connect", lambda **kwargs: conn)


def test_check_connection_reports_a_ready_session(monkeypatch, env_settings):
    conn = session()
    use_session(monkeypatch, conn)

    report = check_connection()

    assert report["user"] == "someone" and report["role"] == "ANALYST"
    assert report["warehouse"] == "WH" and report["warehouse_state"] == "STARTED"
    assert report["auto_resume"] is True
    assert report["problems"] == []
    assert report["key_fingerprint"].startswith("SHA256:")
    assert conn.closed


def test_a_suspended_warehouse_without_auto_resume_is_a_problem_not_a_crash(
    monkeypatch, env_settings
):
    use_session(monkeypatch, session(state="SUSPENDED", auto_resume="false"))

    report = check_connection()

    assert report["auto_resume"] is False
    assert "000608" in report["problems"][0]


def test_a_suspended_warehouse_with_auto_resume_is_fine(monkeypatch, env_settings):
    use_session(monkeypatch, session(state="SUSPENDED", auto_resume="true"))

    assert check_connection()["problems"] == []


def test_no_warehouse_is_a_problem(monkeypatch, env_settings):
    use_session(monkeypatch, session(warehouse=None))

    assert "No warehouse" in check_connection()["problems"][0]


def test_a_warehouse_the_role_cannot_see_is_a_problem(monkeypatch, env_settings):
    use_session(monkeypatch, session(visible=False))

    assert "Grant USAGE" in check_connection()["problems"][0]


def test_the_session_is_closed_when_a_query_fails(monkeypatch, env_settings):
    error = snowflake_error(ProgrammingError, 2003, "Object 'X' does not exist or not authorized.")
    conn = FakeConnection(lambda sql: error)
    use_session(monkeypatch, conn)

    with pytest.raises(SnowflakeConnectorError, match="002003"):
        check_connection()
    assert conn.closed


@pytest.mark.parametrize(
    ("errno", "error_class", "expected"),
    [
        (608, SnowflakeWarehouseError, "auto resume is off"),
        (606, SnowflakeWarehouseError, "No warehouse is set"),
        (2003, SnowflakeConnectorError, "does not exist"),
    ],
)
def test_query_errors_get_the_usual_fix(errno, error_class, expected):
    error = snowflake_error(ProgrammingError, errno, "raw message")
    conn = FakeConnection(lambda sql: error)

    with pytest.raises(error_class, match=expected):
        _query(conn, "SELECT 1")


def test_the_module_logger_is_named_for_the_connector():
    assert module.logger is not None
