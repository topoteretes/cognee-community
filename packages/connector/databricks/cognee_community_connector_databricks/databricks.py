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
