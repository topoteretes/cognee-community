"""DLT source for Snowflake tables (incremental sync + forget-on-delete).

Work in progress. The design, based on tests against a real Snowflake account:

* Key-pair auth with an optional passphrase, plus a connection check that names
  the usual setup mistakes. Key-pair login has to use the user's login name,
  which on a trial account is the email address, not the user name shown in
  Snowsight; with the user name, or with a key that is not registered, Snowflake
  answers only "JWT token is invalid".
* Table and column comments come from SHOW TABLES and SHOW COLUMNS, which run
  without a warehouse. Each table becomes a short description document, so
  cognee knows what the data means.
* Whether a table changed at all comes from SYSTEM$LAST_CHANGE_COMMIT_TIME,
  which also runs without a warehouse and moves on every insert, update and
  delete. An unchanged table is skipped without waking the warehouse. SHOW
  TABLES has no last altered time, and INFORMATION_SCHEMA does need a warehouse.
* Rows of a changed table go down cognee's relational path. With change
  tracking enabled and inside the retention window, only the changes are read
  with CHANGES(INFORMATION => DEFAULT), deletes included. Otherwise a timestamp
  column is the cursor (read with >=, so rows sharing the boundary timestamp
  are never skipped) and deletes come from comparing the primary keys with the
  last sync. A failed query never deletes.

Rows are read with Snowflake's own connector inside a dlt resource, like the
Google Drive and Google Calendar connectors wrap Google's client. dlt's
sql_database source logs in with a key pair too, but it always queries (so it
wakes the warehouse on every sync), never sees deletes, cannot express CHANGES,
and does not map Snowflake's TIMESTAMP_NTZ type.
"""

import base64
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("snowflake_connector")


class SnowflakeConnectorError(RuntimeError):
    """Snowflake refused a request. Messages never carry the key, passphrase or login name."""


class SnowflakeAuthError(SnowflakeConnectorError):
    """The account, login name, key, passphrase or role is not right."""


class SnowflakeWarehouseError(SnowflakeConnectorError):
    """No usable warehouse for a query that needs one."""


def snowflake_source(*args, **kwargs):
    """Create a dlt source that yields Snowflake rows and table descriptions."""
    raise NotImplementedError("The Snowflake connector is still being built.")


def check_connection(**settings) -> dict:
    """Log in once and report what a sync would run with.

    Takes the same settings as the source (or the ``SNOWFLAKE_*`` environment
    variables). Login problems raise ``SnowflakeAuthError`` with the likely fix.
    Warehouse problems do not raise, because a sync where nothing changed needs
    no warehouse; they are listed under ``problems`` instead.

    Returns ``{"user", "role", "warehouse", "warehouse_state", "auto_resume",
    "key_fingerprint", "problems"}``.
    """
    resolved = _resolve_settings(**settings)
    conn = _connect(resolved)
    try:
        who = _query(
            conn, "SELECT CURRENT_USER() AS U, CURRENT_ROLE() AS R, CURRENT_WAREHOUSE() AS W"
        )
        report = {
            "user": who[0]["U"],
            "role": who[0]["R"],
            "warehouse": who[0]["W"],
            "warehouse_state": None,
            "auto_resume": None,
            "key_fingerprint": resolved.key_fingerprint,
            "problems": [],
        }
        report["problems"] = _warehouse_problems(conn, report)
        return report
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Settings, key and login (module-private)
# ---------------------------------------------------------------------------

_ENV = {
    "account": "SNOWFLAKE_ACCOUNT",
    "user": "SNOWFLAKE_USER",
    "private_key": "SNOWFLAKE_PRIVATE_KEY",
    "private_key_path": "SNOWFLAKE_PRIVATE_KEY_PATH",
    "private_key_passphrase": "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
    "role": "SNOWFLAKE_ROLE",
    "warehouse": "SNOWFLAKE_WAREHOUSE",
}

_LOGIN_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class _Settings:
    account: str
    user: str
    # DER encoded PKCS#8 private key, kept out of repr so it never reaches a log.
    private_key: bytes = field(repr=False)
    key_fingerprint: str = ""
    role: str | None = None
    warehouse: str | None = None


def _resolve_settings(
    account: str | None = None,
    user: str | None = None,
    private_key: str | bytes | None = None,
    private_key_path: str | None = None,
    private_key_passphrase: str | None = None,
    role: str | None = None,
    warehouse: str | None = None,
) -> _Settings:
    """Merge arguments with ``SNOWFLAKE_*`` environment variables, and load the key.

    Fails before any network call, naming every missing setting.
    """
    given = {
        "account": account,
        "user": user,
        "private_key": private_key,
        "private_key_path": private_key_path,
        "private_key_passphrase": private_key_passphrase,
        "role": role,
        "warehouse": warehouse,
    }
    values = {name: value or os.environ.get(_ENV[name]) or None for name, value in given.items()}

    missing = [f"{name} ({_ENV[name]})" for name in ("account", "user") if not values[name]]
    if not values["private_key"] and not values["private_key_path"]:
        missing.append(
            "private_key (SNOWFLAKE_PRIVATE_KEY) or private_key_path (SNOWFLAKE_PRIVATE_KEY_PATH)"
        )
    if missing:
        raise ValueError("Snowflake settings are missing: " + ", ".join(missing) + ".")

    pem = values["private_key"]
    if not pem:
        path = os.path.expanduser(values["private_key_path"])
        try:
            with open(path, "rb") as key_file:
                pem = key_file.read()
        except OSError as error:
            raise SnowflakeAuthError(
                f"The private key file could not be read ({error.strerror})."
            ) from None
    der, fingerprint = _load_private_key(pem, values["private_key_passphrase"])

    return _Settings(
        account=_normalize_account(values["account"]),
        user=values["user"].strip(),
        private_key=der,
        key_fingerprint=fingerprint,
        role=values["role"],
        warehouse=values["warehouse"],
    )


def _normalize_account(account: str) -> str:
    """Accept the identifier as typed or pasted from the browser.

    ``https://ORG-ACCT.snowflakecomputing.com/`` and ``ORG-ACCT`` both become
    ``ORG-ACCT``.
    """
    cleaned = account.strip()
    cleaned = re.sub(r"^https?://", "", cleaned, flags=re.IGNORECASE).rstrip("/")
    cleaned = re.sub(r"\.snowflakecomputing\.com$", "", cleaned, flags=re.IGNORECASE)
    if not cleaned or any(character.isspace() or character == "/" for character in cleaned):
        raise ValueError(
            "The Snowflake account identifier looks wrong. It has the form ORGNAME-ACCOUNTNAME, "
            "shown in Snowsight under your name, then Account, then View account details."
        )
    return cleaned


def _load_private_key(pem: str | bytes, passphrase: str | None) -> tuple[bytes, str]:
    """Return the key as DER bytes, and the fingerprint Snowflake shows for it.

    The fingerprint is what ``DESC USER`` lists as RSA_PUBLIC_KEY_FP, so a
    mismatch can be spotted without ever printing the key.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

    data = pem.encode() if isinstance(pem, str) else pem
    password = passphrase.encode() if passphrase else None
    try:
        key = serialization.load_pem_private_key(data, password=password)
    except TypeError as error:
        if "not encrypted" in str(error):
            raise SnowflakeAuthError(
                "A passphrase was given, but the private key is not encrypted. "
                "Remove the passphrase."
            ) from None
        raise SnowflakeAuthError(
            "The private key is encrypted. Pass private_key_passphrase or set "
            "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE."
        ) from None
    except ValueError:
        raise SnowflakeAuthError(
            "The private key could not be read. It must be a PEM key (it starts with "
            "-----BEGIN), and the passphrase, if any, must match."
        ) from None
    if not isinstance(key, RSAPrivateKey):
        raise SnowflakeAuthError("Snowflake key-pair login needs an RSA key (2048 bits or more).")

    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(public).digest()).decode()
    return der, fingerprint


def _connect(settings: _Settings, connect: Callable[..., Any] | None = None):
    """Open a key-pair session, turning Snowflake's login errors into fixes."""
    if connect is None:
        import snowflake.connector

        connect = snowflake.connector.connect
        # The driver logs a version banner on every login at INFO. Keep it quiet
        # unless the application configured that logger itself.
        driver_logger = logging.getLogger("snowflake.connector")
        if driver_logger.level == logging.NOTSET:
            driver_logger.setLevel(logging.WARNING)
    try:
        return connect(
            account=settings.account,
            user=settings.user,
            private_key=settings.private_key,
            authenticator="SNOWFLAKE_JWT",
            role=settings.role,
            warehouse=settings.warehouse,
            login_timeout=_LOGIN_TIMEOUT_SECONDS,
            client_session_keep_alive=False,
        )
    except Exception as error:
        raise _friendly_error(error, settings.key_fingerprint) from error


def _query(conn, sql: str, params: Any = None) -> list[dict]:
    """Run one statement and return its rows as dicts, with friendly errors."""
    from snowflake.connector import DictCursor

    try:
        with conn.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else []
    except Exception as error:
        raise _friendly_error(error) from error


def _friendly_error(error: Exception, key_fingerprint: str = "") -> Exception:
    """Map Snowflake's error codes, checked on a real account, to the usual fix."""
    errno = getattr(error, "errno", None)
    message = str(getattr(error, "msg", None) or error)

    if errno == 390144:
        # The same code covers two different mistakes, so name both.
        return SnowflakeAuthError(
            "Snowflake refused the key-pair login (390144, JWT token is invalid). It reports two "
            "different mistakes this way. First, user must be the login name, which on trial "
            "accounts is your email address, not the user name shown in Snowsight: check "
            "LOGIN_NAME in DESC USER. Second, the public key registered on that user must belong "
            f"to this private key: RSA_PUBLIC_KEY_FP in DESC USER should be {key_fingerprint}."
        )
    if errno == 390189:
        return SnowflakeAuthError(
            "The role is not granted to this user (390189). Leave role empty to use the user's "
            "default role, or grant it with GRANT ROLE <role> TO USER <user>."
        )
    if "404 Not Found" in message and errno in (250001, 290404):
        # Connector 4.x reports an unknown account as 290404, 5.x as 250001.
        return SnowflakeAuthError(
            "Snowflake has no account with this identifier (HTTP 404). It has the form "
            "ORGNAME-ACCOUNTNAME, shown in Snowsight under your name, then Account, then View "
            "account details."
        )
    if errno == 608:
        return SnowflakeWarehouseError(
            "The warehouse is suspended and auto resume is off (000608). Resume it, or run "
            "ALTER WAREHOUSE <name> SET AUTO_RESUME = TRUE."
        )
    if errno == 606:
        return SnowflakeWarehouseError(
            "No warehouse is set for this session (000606). Pass warehouse or set "
            "SNOWFLAKE_WAREHOUSE, or give the user a default warehouse."
        )
    if errno == 2003:
        return SnowflakeConnectorError(
            "A selected object does not exist, or this role cannot see it (002003). Check the "
            "database, schema and table names, and the role's grants."
        )
    first_line = message.strip().splitlines()[0] if message.strip() else type(error).__name__
    # Snowflake's own text usually starts with the code already: "123456 (08001): ...".
    if errno is not None and not first_line.startswith(str(errno)):
        first_line = f"{errno}: {first_line}"
    return SnowflakeConnectorError(f"Snowflake error {first_line}")


def _warehouse_problems(conn, report: dict) -> list[str]:
    """Fill in the warehouse state and say what would stop a query from running."""
    name = report["warehouse"]
    if not name:
        return [
            "No warehouse is set. Unchanged tables still sync, but reading rows needs one: pass "
            "warehouse or set SNOWFLAKE_WAREHOUSE."
        ]
    pattern = name.replace("'", "''")
    rows = [r for r in _query(conn, f"SHOW WAREHOUSES LIKE '{pattern}'") if r.get("name") == name]
    if not rows:
        return ["This role cannot see the warehouse. Grant USAGE on it to the role."]
    report["warehouse_state"] = rows[0].get("state")
    report["auto_resume"] = str(rows[0].get("auto_resume")).lower() == "true"
    if report["warehouse_state"] == "SUSPENDED" and not report["auto_resume"]:
        return [
            "The warehouse is suspended and auto resume is off, so reading rows would fail with "
            "000608. Unchanged tables still sync. Resume it, or run ALTER WAREHOUSE <name> SET "
            "AUTO_RESUME = TRUE."
        ]
    return []


# ---------------------------------------------------------------------------
# Table descriptions: one document per table, read without a warehouse.
#
# SHOW TABLES, SHOW COLUMNS, SHOW PRIMARY KEYS and SHOW IMPORTED KEYS run in
# Snowflake's cloud services layer, so describing a whole schema takes four
# calls and never wakes the warehouse. Only text people wrote and the table's
# shape go into the document, never row counts, sizes or dates, so a table
# whose comments and columns did not change renders to the same text.
# ---------------------------------------------------------------------------

ID_PREFIX = "snowflake:"


@dataclass(frozen=True, order=True)
class _TableRef:
    database: str
    schema: str
    name: str

    @property
    def fqn(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}"

    @property
    def sql(self) -> str:
        return ".".join(_quote(part) for part in (self.database, self.schema, self.name))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _parse_table(reference: str) -> _TableRef:
    """``DB.SCHEMA.TABLE`` as a user would type it in SQL.

    Unquoted parts are upper-cased, as Snowflake does; parts in double quotes
    are kept exactly, so lowercase or dotted names still work.
    """
    parts, current, quoted, index = [], "", False, 0
    text = reference.strip()
    while index < len(text):
        character = text[index]
        if character == '"':
            if quoted and text[index + 1 : index + 2] == '"':
                current += '"'
                index += 1
            else:
                quoted = not quoted
                current += "\0" if quoted else ""
        elif character == "." and not quoted:
            parts.append(current)
            current = ""
        else:
            current += character
        index += 1
    parts.append(current)

    names = []
    for part in parts:
        if part.startswith("\0"):
            names.append(part[1:])
        else:
            names.append(part.strip().upper())
    if quoted or len(names) != 3 or not all(names):
        raise ValueError(
            f"Tables are written DATABASE.SCHEMA.TABLE, like SALES_DB.PUBLIC.ORDERS: {reference!r}"
        )
    return _TableRef(*names)


def _describe_tables(conn, refs: list[_TableRef]) -> tuple[list[dict], list[_TableRef]]:
    """Description rows for the tables that exist, and the refs that do not.

    A ref is only reported missing when its schema was listed completely;
    an error listing a schema raises, so it can never look like a dropped table.
    """
    rows, missing = [], []
    schema_comments: dict[str, dict[str, str]] = {}
    for database, schema in sorted({(ref.database, ref.schema) for ref in refs}):
        if database not in schema_comments:
            schema_comments[database] = {
                row["name"]: row.get("comment") or ""
                for row in _query(conn, f"SHOW SCHEMAS IN DATABASE {_quote(database)}")
            }
        meta = _schema_metadata(conn, database, schema)
        for ref in sorted(r for r in refs if (r.database, r.schema) == (database, schema)):
            if ref.name not in meta["tables"]:
                missing.append(ref)
                continue
            rows.append(
                _render_table(
                    ref,
                    meta["tables"][ref.name],
                    schema_comments[database].get(schema, ""),
                    meta["columns"].get(ref.name, []),
                    meta["primary_keys"].get(ref.name, []),
                    meta["foreign_keys"].get(ref.name, []),
                )
            )
    return rows, missing


def _schema_metadata(conn, database: str, schema: str) -> dict:
    """Tables, columns and declared keys of one schema, four SHOW calls."""
    where = f"{_quote(database)}.{_quote(schema)}"
    columns: dict[str, list[dict]] = {}
    for row in _query(conn, f"SHOW COLUMNS IN SCHEMA {where}"):
        columns.setdefault(row["table_name"], []).append(row)
    primary_keys: dict[str, list[dict]] = {}
    for row in _query(conn, f"SHOW PRIMARY KEYS IN SCHEMA {where}"):
        primary_keys.setdefault(row["table_name"], []).append(row)
    foreign_keys: dict[str, list[dict]] = {}
    for row in _query(conn, f"SHOW IMPORTED KEYS IN SCHEMA {where}"):
        foreign_keys.setdefault(row["fk_table_name"], []).append(row)
    return {
        "tables": {row["name"]: row for row in _query(conn, f"SHOW TABLES IN SCHEMA {where}")},
        "columns": columns,
        "primary_keys": {
            table: [r["column_name"] for r in sorted(keys, key=lambda r: int(r["key_sequence"]))]
            for table, keys in primary_keys.items()
        },
        "foreign_keys": foreign_keys,
    }


def _render_table(ref, table, schema_comment, columns, primary_key, foreign_keys) -> dict:
    """One document describing what a table holds and how it links to others."""
    lines = [f"Snowflake table {ref.fqn}"]
    comment = " ".join(str(table.get("comment") or "").split())
    if comment:
        lines.append(comment)
    if schema_comment:
        lines.append(f"Schema {ref.database}.{ref.schema}: {' '.join(schema_comment.split())}")
    if primary_key:
        lines.extend(["", "Primary key: " + ", ".join(primary_key)])

    lines.extend(["", "Columns:"])
    for column in columns:
        line = f"{column['column_name']} ({_column_type(column.get('data_type'))}"
        line += ", required)" if column.get("null?") == "NOT_NULL" else ")"
        column_comment = " ".join(str(column.get("comment") or "").split())
        lines.append(f"{line}: {column_comment}" if column_comment else line)

    links = _foreign_key_lines(foreign_keys)
    if links:
        lines.extend(["", "References:", *links])

    return {
        "id": f"{ID_PREFIX}table:{ref.fqn}",
        "title": ref.fqn,
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _foreign_key_lines(foreign_keys: list[dict]) -> list[str]:
    """``ORDERS.CUSTOMER_ID -> CUSTOMERS.CUSTOMER_ID``, one line per constraint."""
    by_constraint: dict[str, list[dict]] = {}
    for row in foreign_keys:
        by_constraint.setdefault(row.get("fk_name") or "", []).append(row)
    lines = []
    for _, parts in sorted(by_constraint.items()):
        parts = sorted(parts, key=lambda r: int(r.get("key_sequence") or 0))
        first = parts[0]
        target = f"{first['pk_database_name']}.{first['pk_schema_name']}.{first['pk_table_name']}"
        own = ", ".join(p["fk_column_name"] for p in parts)
        theirs = ", ".join(p["pk_column_name"] for p in parts)
        lines.append(f"{own} references {target} ({theirs})")
    return lines


def _column_type(data_type: Any) -> str:
    """Readable SQL type from SHOW COLUMNS' JSON, like NUMBER(12,2) or VARCHAR(20)."""
    try:
        info = json.loads(data_type) if isinstance(data_type, str) else dict(data_type or {})
    except (TypeError, ValueError):
        return str(data_type or "unknown")
    kind = str(info.get("type") or "unknown")
    if kind == "FIXED":
        return f"NUMBER({info.get('precision', 38)},{info.get('scale', 0)})"
    if kind == "TEXT":
        length = info.get("length")
        return f"VARCHAR({length})" if length and length < 16777216 else "VARCHAR"
    return {"REAL": "FLOAT"}.get(kind, kind)
