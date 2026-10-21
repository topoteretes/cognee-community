"""DLT source for Databricks notebooks and Unity Catalog tables (incremental + forget-on-delete).

Work in progress. The planned design, checked against a real Free Edition
workspace:

* Notebooks from the selected workspace folders are exported as source, which
  never carries cell outputs. Markdown cells stay prose and code cells stay
  code, and hardcoded tokens or passwords are scrubbed.
* Each Unity Catalog table in the selected catalogs and schemas becomes a short
  document: description, columns with types and comments, keys, owner, and the
  view definition for views.
* Notebooks and tables are joined through the lineage API, which names the
  notebooks that read and write each table by their workspace id. Table names
  found in notebook text are the fallback for notebooks that never ran.
* Query results are off by default. Only queries the user names are run, on a
  warehouse the user names, with a row cap and a timeout.
* Every sync lists the selected folders and schemas in full. The listing carries
  each notebook's modified time and each table's updated time, so only what
  changed is exported or re-read, and records are tracked by their permanent id
  so a rename or a move is not a delete. Something missing from a complete
  listing is looked up once more before it is forgotten, and a failed listing
  never deletes.
"""

import base64
import re
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlparse

from cognee.shared.logging_utils import get_logger
from dlt.sources.helpers.requests.retry import Client
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator

logger = get_logger("databricks_connector")


class DatabricksConnectorError(RuntimeError):
    """Databricks refused or failed a request.

    Messages carry the HTTP status and Databricks' error code only. Databricks'
    own messages repeat the request's path, which holds the user's email for
    anything under /Users, so they are never copied into errors or logs.
    """


class DatabricksAuthError(DatabricksConnectorError):
    """The token is missing, malformed, expired, or not allowed to do this."""


class DatabricksNotFoundError(DatabricksConnectorError):
    """Databricks says the folder, notebook, schema or table does not exist."""


def databricks_source(*args, **kwargs):
    """Create a dlt source that yields Databricks notebooks and tables as documents."""
    raise NotImplementedError("The Databricks connector is still being built.")


# ---------------------------------------------------------------------------
# REST client (module-private)
# ---------------------------------------------------------------------------

# The codes Databricks answers a missing object with, under HTTP 404: the
# workspace API says RESOURCE_DOES_NOT_EXIST and Unity Catalog names the kind.
# Checked on a Free Edition workspace.
_NOT_FOUND_CODES = {
    "RESOURCE_DOES_NOT_EXIST",
    "CATALOG_DOES_NOT_EXIST",
    "SCHEMA_DOES_NOT_EXIST",
    "TABLE_DOES_NOT_EXIST",
}

# Every response names the workspace it came from in this header; it ties the
# saved state to one workspace without an extra call.
_WORKSPACE_HEADER = "x-databricks-org-id"


class _DatabricksClient:
    """A thin client over dlt's ``RESTClient`` for the Databricks REST API.

    dlt's retrying session retries HTTP 429 and 5xx and honours ``Retry-After``.
    Every response goes through ``_raise_for_errors``, so an error can never be
    read as an empty answer.
    """

    def __init__(
        self,
        host: str,
        token: str,
        session: Any = None,
        check_active: Callable[[], None] | None = None,
    ):
        self.host = _clean_host(host)
        self._rest = RESTClient(
            base_url=self.host,
            auth=BearerTokenAuth(_clean_token(token)),
            session=session or _retrying_session(),
        )
        # A host's authorization checkpoint, run before every request so a
        # revoked connection stops the sync at the next call, not at its end.
        self._check_active = check_active or (lambda: None)
        self.workspace_id: str | None = None

    def get(self, path: str, params: dict | None = None) -> dict:
        """One GET request, returning its JSON object."""
        self._check_active()
        response = self._rest.get(path, params=params or {})
        self._on_response(response)
        return response.json()

    def pages(self, path: str, params: dict, items_key: str) -> Iterator[dict]:
        """Yield the items of a listing across ``next_page_token`` pages.

        Databricks leaves the list out entirely when it is empty, so a missing
        ``items_key`` reads as no items. A value that is there but is not a list
        raises instead.
        """
        paginator = JSONResponseCursorPaginator(
            cursor_path="next_page_token", cursor_param="page_token"
        )

        def require_list(response, *args, **kwargs):
            found = response.json().get(items_key, [])
            if not isinstance(found, list):
                raise DatabricksConnectorError(
                    f"Databricks answered a listing whose {items_key} is not a list."
                )

        pages = self._rest.paginate(
            path,
            method="GET",
            params=dict(params),
            paginator=paginator,
            data_selector=items_key,
            hooks={"response": [self._on_response, require_list]},
        )
        while True:
            # Pages are requested lazily, so this runs before each request.
            self._check_active()
            page = next(pages, None)
            if page is None:
                return
            yield from page

    def _on_response(self, response, *args, **kwargs) -> None:
        _raise_for_errors(response)
        workspace = response.headers.get(_WORKSPACE_HEADER)
        if workspace:
            self.workspace_id = workspace


def _retrying_session(**client_kwargs):
    """dlt's retrying session; ``client_kwargs`` override its retry settings."""
    return Client(raise_for_status=False, **client_kwargs).session


def _raise_for_errors(response, *args, **kwargs) -> None:
    """Raise on an HTTP error or an answer that is not a JSON object."""
    status = response.status_code
    if status == 200:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise DatabricksConnectorError("Databricks returned a response that is not JSON.")
        return

    code = _error_code(response)
    if status in (401, 403):
        raise DatabricksAuthError(
            f"Databricks refused the token (HTTP {status}). Check that it is valid, has not "
            "expired, and that its user can read the selected folders and schemas."
        )
    if status == 404 and code in _NOT_FOUND_CODES:
        raise DatabricksNotFoundError(f"Databricks says this does not exist ({code}).")
    if status == 429:
        raise DatabricksConnectorError(
            f"Databricks rate limit still exceeded after retrying (HTTP 429, {code})."
        )
    raise DatabricksConnectorError(f"Databricks returned HTTP {status} ({code}).")


def _error_code(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "no error code"
    code = body.get("error_code") if isinstance(body, dict) else None
    return str(code) if code else "no error code"


def _clean_host(host: str | None) -> str:
    """The workspace URL as ``https://<host>``.

    A URL copied from the browser carries a path and login parameters, like
    ``?o=<workspace id>``; only the host is kept.
    """
    text = (host or "").strip()
    if not text:
        raise ValueError(
            "A Databricks workspace URL is required, like https://dbc-1234.cloud.databricks.com."
        )
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("The Databricks workspace URL must start with https://.")
    return f"https://{parsed.netloc}"


def _clean_token(token: str | None) -> str:
    """Validate a personal access token without ever echoing it."""
    cleaned = (token or "").strip()
    if not cleaned:
        raise DatabricksAuthError("A Databricks personal access token is required.")
    if not cleaned.isascii() or any(character.isspace() for character in cleaned):
        raise DatabricksAuthError("The Databricks token contains spaces or non-ASCII characters.")
    return cleaned


# ---------------------------------------------------------------------------
# Notebooks: one document per notebook, exported as source.
#
# Every sync walks the selected folders in full. The listing gives each
# notebook's permanent object_id and its modified_at, so only notebooks whose
# modified_at moved are exported, and a notebook that was moved or renamed
# keeps its id. Export format SOURCE is the notebook's code and markdown only:
# cell outputs, which can hold real rows and credentials, are never read.
# ---------------------------------------------------------------------------

ID_PREFIX = "databricks:"

# Folders the walk skips: the workspace's own bookkeeping folders start with a
# dot, like the .db_internal folder every home folder has.
_HIDDEN_PREFIX = "."

# Workspace objects the walk descends into. A Git folder lists like a folder.
_FOLDER_TYPES = {"DIRECTORY", "REPO"}

_LANGUAGES = {"PYTHON": "python", "SQL": "sql", "SCALA": "scala", "R": "r"}

# Magic commands that turn a cell into another language, and the label its
# code block gets.
_MAGIC_LANGUAGES = {
    "%python": "python",
    "%sql": "sql",
    "%scala": "scala",
    "%r": "r",
    "%sh": "sh",
    "%pip": "sh",
    "%fs": "sh",
    "%run": "text",
}


def _sync_notebooks(
    client, roots: list[str], state: dict, stats: dict, failed: set, full_refresh=False
):
    """Yield a document for every new or changed notebook under ``roots``, and a
    ``_deleted`` row for every notebook that is gone.

    Each root is synced on its own: a root that fails to list keeps its saved
    state and emits nothing, so nothing under it is forgotten, while the other
    roots finish. Roots that failed are added to ``failed``.
    """
    saved = state.setdefault("notebooks", {})

    # Notebooks under a root that is no longer selected go with it.
    for object_id in sorted(i for i, n in saved.items() if n.get("root") not in roots):
        yield from _forget_notebook(object_id, saved, stats)

    claimed: set[str] = set()
    for root in roots:
        root_stats: dict[str, int] = {}
        try:
            # Collected first, so a root that fails half way emits nothing and
            # its saved state stays as it was.
            documents, updates, gone = _sync_root(
                client, root, saved, root_stats, full_refresh, claimed
            )
        except DatabricksAuthError:
            raise
        except DatabricksConnectorError as error:
            failed.add(root)
            logger.warning(
                "Databricks: a folder failed to sync (%s); it keeps its state and is "
                "retried on the next run.",
                type(error).__name__,
            )
            continue
        for object_id in gone:
            yield from _forget_notebook(object_id, saved, root_stats)
        saved.update(updates)
        for name, count in root_stats.items():
            stats[name] = stats.get(name, 0) + count
        yield from documents


def _sync_root(client, root, saved, stats, full_refresh, claimed):
    """List one root, export what changed, and confirm what is gone."""
    listed = {
        object_id: entry
        for object_id, entry in _walk(client, root).items()
        if object_id not in claimed
    }
    claimed.update(listed)
    stats["scanned"] = stats.get("scanned", 0) + len(listed)

    documents, updates = [], {}
    for object_id, entry in sorted(listed.items()):
        before = saved.get(object_id, {})
        updates[object_id] = {**entry, "root": root}
        if not full_refresh and before.get("modified_at") == entry["modified_at"]:
            # Unchanged, but a move or rename still updates the saved path.
            continue
        try:
            source = _export(client, entry["path"])
        except DatabricksNotFoundError:
            # Deleted between the listing and the export: keep the old stamp,
            # so the next sync sees it gone.
            updates[object_id] = {**before, "root": root} if before else None
            continue
        text, redacted = _render_notebook(entry, source)
        stats["changed"] = stats.get("changed", 0) + 1
        if redacted:
            stats["redacted"] = stats.get("redacted", 0) + redacted
        documents.append(
            {
                "id": f"{ID_PREFIX}notebook:{object_id}",
                "title": entry["path"].rsplit("/", 1)[-1],
                "content": text,
                "_deleted": False,
            }
        )
    updates = {object_id: entry for object_id, entry in updates.items() if entry}

    missing = [i for i, n in saved.items() if n.get("root") == root and i not in listed]
    gone = [i for i in sorted(missing) if _confirm_gone(client, i, saved[i]["path"])]
    return documents, updates, gone


def _walk(client, root: str) -> dict[str, dict]:
    """Every notebook under ``root``, by object_id. Any folder that fails raises."""
    found: dict[str, dict] = {}
    folders = [root]
    while folders:
        folder = folders.pop()
        answer = client.get("/api/2.0/workspace/list", {"path": folder})
        objects = answer.get("objects", [])
        if not isinstance(objects, list):
            raise DatabricksConnectorError(
                "Databricks answered a folder listing that is not a list."
            )
        for obj in objects:
            name = str(obj.get("path", "")).rsplit("/", 1)[-1]
            kind = obj.get("object_type")
            if kind in _FOLDER_TYPES and not name.startswith(_HIDDEN_PREFIX):
                folders.append(obj["path"])
            elif kind == "NOTEBOOK":
                found[str(obj["object_id"])] = {
                    "path": obj["path"],
                    "language": obj.get("language") or "",
                    "modified_at": obj.get("modified_at"),
                }
    return found


def _confirm_gone(client, object_id: str, path: str) -> bool:
    """Whether a notebook missing from a complete listing is really gone.

    Its last known path is looked up once more. Gone means Databricks says the
    path does not exist, or something else now sits there. The same notebook
    still at that path contradicts the listing, so it is kept rather than
    forgotten on one signal. Any other error raises, so the root fails and
    nothing is forgotten.
    """
    try:
        status = client.get("/api/2.0/workspace/get-status", {"path": path})
    except DatabricksNotFoundError:
        return True
    return str(status.get("object_id")) != object_id


def _export(client, path: str) -> str:
    answer = client.get("/api/2.0/workspace/export", {"path": path, "format": "SOURCE"})
    content = answer.get("content")
    if not isinstance(content, str):
        raise DatabricksConnectorError("Databricks answered an export without its content.")
    return base64.b64decode(content).decode("utf-8", errors="replace")


_LANGUAGE_NAMES = {"PYTHON": "Python", "SQL": "SQL", "SCALA": "Scala", "R": "R"}


def _render_notebook(entry: dict, source: str) -> tuple[str, int]:
    """A notebook as text: its name, folder and language, then markdown cells as
    prose and code cells as code blocks. Returns the text and how many secrets
    were scrubbed out of it.

    Only the exported source is used, never modified times or authors, so an
    unchanged notebook renders to the same text.
    """
    path = entry["path"]
    name = path.rsplit("/", 1)[-1]
    language = entry.get("language") or ""
    header = (
        f"Databricks notebook {name}\n"
        f"Folder: {_display_path(path.rsplit('/', 1)[0])}\n"
        f"Language: {_LANGUAGE_NAMES.get(language, language.title() or 'unknown')}"
    )
    parts = [header]
    for kind, text in _cells(source, _LANGUAGES.get(language, "text")):
        parts.append(text if kind == "markdown" else f"```{kind}\n{text}\n```")
    return _scrub("\n\n".join(parts))


def _cells(source: str, language: str) -> list[tuple[str, str]]:
    """The cells of a SOURCE export, as ``(kind, text)``.

    ``kind`` is "markdown" or a code language. The export marks the file with a
    "Databricks notebook source" line, separates cells with "COMMAND ----------"
    and writes magic cells (``%md``, ``%sql``, ...) as comment lines starting
    with "MAGIC", all in the notebook language's comment syntax.
    """
    lines = source.splitlines()
    header = re.match(r"^(#|--|//) Databricks notebook source\s*$", lines[0]) if lines else None
    if not header:
        return [(language, source.strip())] if source.strip() else []
    comment = header.group(1)
    raw_cells: list[list[str]] = [[]]
    for line in lines[1:]:
        if line.strip() == f"{comment} COMMAND ----------":
            raw_cells.append([])
        else:
            raw_cells[-1].append(line)

    cells = []
    for raw in raw_cells:
        while raw and not raw[0].strip():
            raw = raw[1:]
        title = None
        if raw and re.match(rf"^{re.escape(comment)} DBTITLE \d+,", raw[0]):
            title = raw[0].split(",", 1)[1].strip()
            raw = raw[1:]
        filled = [line for line in raw if line.strip()]
        if not filled:
            continue
        magic = f"{comment} MAGIC"
        if title:
            cells.append(("markdown", title))
        if all(line.startswith(magic) for line in filled):
            cells.append(_magic_cell([line[len(magic) :].removeprefix(" ") for line in raw]))
        else:
            cells.append((language, "\n".join(raw).strip("\n")))
    return [(kind, text) for kind, text in cells if text.strip()]


def _magic_cell(lines: list[str]) -> tuple[str, str]:
    text = "\n".join(lines).strip("\n")
    command, _, rest = text.partition("\n")
    word = command.split(maxsplit=1)[0] if command.strip() else ""
    if word in ("%md", "%md-sandbox"):
        first = command[len(word) :].strip()
        return "markdown", "\n".join(part for part in (first, rest) if part).strip("\n")
    if word == "%run":
        return "text", command.strip()
    if word in _MAGIC_LANGUAGES:
        first = command[len(word) :].strip()
        return _MAGIC_LANGUAGES[word], "\n".join(part for part in (first, rest) if part).strip("\n")
    return "text", text


def _display_path(path: str) -> str:
    """A workspace path without the user's email: /Users/<email>/x reads ~/x."""
    shown = "/".join("~" if "@" in part else part for part in path.split("/"))
    return "~" + shown[len("/Users/~") :] if shown.startswith("/Users/~") else shown


# ---------------------------------------------------------------------------
# Secret scrubbing. Notebooks are code, and code sometimes holds a credential
# typed in by hand. Anything that looks like one is replaced before the text
# leaves the connector. A false positive only hides a harmless value.
# ---------------------------------------------------------------------------

_REDACTED = "[REDACTED]"

_SECRET_PATTERNS = [
    # Private key blocks, PEM encoded.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    # Databricks personal access tokens.
    re.compile(r"\bdapi[0-9a-f]{32}(?:-\d+)?\b"),
    # AWS access key ids.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # OpenAI, Anthropic and similar "sk-" keys.
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    # GitHub tokens.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    # Slack tokens.
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    # Google API keys.
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
]

# A quoted value given to a name that says it holds a secret, in code or config:
# password = "...", "api_key": "...", spark.conf.set("fs.azure.account.key", "...").
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key
        |account[_.-]?key|private[_-]?key|credential)s?[\w.-]*\b["']?\s*(?:[:=,]|=>)\s*)
    (["'])(?P<value>[^"'\n]{4,})\2
    """
)

# Bearer tokens written into headers.
_BEARER = re.compile(r"(?i)(\bBearer\s+)(?P<value>[A-Za-z0-9._~+/=-]{8,})")

# The password inside a connection string: scheme://user:password@host.
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<value>[^\s@/]+)(@)")


def _scrub(text: str) -> tuple[str, int]:
    """Replace anything that looks like a credential; return the text and the count.

    A value an earlier pattern already replaced is not counted twice.
    """
    count = 0

    def redact(match, keep_before, keep_after):
        nonlocal count
        if match.group("value") == _REDACTED:
            return match.group(0)
        count += 1
        before = "".join(match.group(group) for group in keep_before)
        after = "".join(match.group(group) for group in keep_after)
        return f"{before}{_REDACTED}{after}"

    for pattern in _SECRET_PATTERNS:
        text, found = pattern.subn(_REDACTED, text)
        count += found
    text = _SECRET_ASSIGNMENT.sub(lambda m: redact(m, (1, 2), (2,)), text)
    text = _BEARER.sub(lambda m: redact(m, (1,), ()), text)
    text = _URL_PASSWORD.sub(lambda m: redact(m, (1,), (3,)), text)
    return text, count


def _forget_notebook(object_id: str, saved: dict, stats: dict) -> Iterator[dict]:
    """A ``_deleted`` row, which makes cognee forget the document on merge."""
    saved.pop(object_id, None)
    stats["deleted"] = stats.get("deleted", 0) + 1
    yield {"id": f"{ID_PREFIX}notebook:{object_id}", "_deleted": True}
