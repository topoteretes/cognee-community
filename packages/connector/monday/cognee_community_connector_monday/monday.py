"""DLT source for monday.com boards (incremental sync + forget-on-delete).

Work in progress. The planned design, based on tests against a real account:

* Each item becomes one document on cognee's document path, in the same shape
  as the Linear connector: a state line, an assignee line, a parent line for
  sub-items, the other columns, and the item's updates folded in.
* Every sync lists the ids and last-updated times of all active items, and
  fetches full details only for items that changed.
* Posting or editing an update does not change an item's last-updated time and
  is not in the activity log, so updates are read as their own stream.
* An id missing from a complete listing is looked up by id. Archived or deleted
  items get a ``_deleted`` marker so cognee forgets them. A failed or partial
  listing never deletes anything.

monday.com has a single GraphQL endpoint, so the declarative ``rest_api`` source
does not fit the sync above (list, compare, fetch what changed, confirm what is
gone). The client is still built on dlt's REST toolkit: ``RESTClient`` for the
requests, dlt's retrying session for rate limits and server errors, and dlt's
cursor paginator, which writes monday.com's page cursor into the query variables.
"""

from collections.abc import Iterator
from typing import Any

from dlt.sources.helpers.requests.retry import Client
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator

API_URL = "https://api.monday.com/v2"
# Pinned so a schema change upstream can't silently alter parsing. 2026-07 is the
# current stable version, and every query in this module was checked against it.
API_VERSION = "2026-07"

# GraphQL error codes that mean the token itself is not usable.
_AUTH_ERROR_CODES = {"NOT_AUTHENTICATED", "UNAUTHORIZED", "USER_UNAUTHORIZED"}


class MondayAPIError(RuntimeError):
    """monday.com rejected a request. Messages never carry the token or variables."""


class MondayAuthError(MondayAPIError):
    """The token is missing, malformed, revoked, or lacks access."""


def monday_source(*args, **kwargs):
    """Create a dlt source that yields monday.com items as documents."""
    raise NotImplementedError("The monday.com connector is still being built.")


# ---------------------------------------------------------------------------
# GraphQL client (module-private)
# ---------------------------------------------------------------------------


class _MondayClient:
    """A thin GraphQL client over dlt's ``RESTClient``.

    monday.com reports most failures inside an HTTP 200 response, as an
    ``errors`` list next to (or instead of) ``data``. Every response goes through
    ``_raise_for_errors`` so such a reply can never be read as an empty result.
    """

    def __init__(self, token: str, session: Any = None):
        headers = {"Authorization": _clean_token(token), "API-Version": API_VERSION}
        self._rest = RESTClient(
            base_url=API_URL, headers=headers, session=session or _retrying_session()
        )

    def query(self, query: str, variables: dict | None = None) -> dict:
        """Run one query and return its ``data``."""
        response = self._rest.request(
            "", method="POST", json={"query": query, "variables": variables or {}}
        )
        _raise_for_errors(response)
        return response.json()["data"]

    def paginate(
        self, query: str, variables: dict, cursor_path: str, items_path: str
    ) -> Iterator[dict]:
        """Yield items across cursor pages of one query.

        The query must take a ``$cursor`` variable and return the next cursor at
        ``cursor_path``; a null cursor ends the walk.
        """
        paginator = JSONResponseCursorPaginator(
            cursor_path=cursor_path, cursor_body_path="variables.cursor"
        )
        pages = self._rest.paginate(
            "",
            method="POST",
            json={"query": query, "variables": dict(variables)},
            paginator=paginator,
            data_selector=items_path,
            hooks={"response": [_raise_for_errors]},
        )
        for page in pages:
            yield from page


def _retrying_session(**client_kwargs):
    """dlt's retrying session, also retrying monday.com's in-body rate limit errors.

    HTTP 429 and 5xx are retried by dlt with backoff, honoring ``Retry-After``.
    A rate limit can also arrive as a 200 whose error carries ``retry_in_seconds``.
    ``client_kwargs`` override dlt's retry settings (tests turn the backoff off).
    """
    return Client(raise_for_status=False, retry_condition=_is_rate_limited, **client_kwargs).session


def _is_rate_limited(response, exception) -> bool:
    if response is None or response.status_code != 200:
        return False
    return any("retry_in_seconds" in _extensions(error) for error in _errors(response))


def _raise_for_errors(response, *args, **kwargs) -> None:
    """Raise on an HTTP error or a GraphQL error; return quietly on success."""
    if response.status_code in (401, 403):
        raise MondayAuthError(
            f"monday.com refused the token (HTTP {response.status_code}). "
            "Check that it is valid and can read the selected boards."
        )
    if response.status_code != 200:
        raise MondayAPIError(f"monday.com returned HTTP {response.status_code}.")

    errors = _errors(response)
    if not errors:
        if not isinstance(_json(response).get("data"), dict):
            raise MondayAPIError("monday.com returned a response without data.")
        return

    codes = sorted({str(_extensions(error).get("code") or "UNKNOWN") for error in errors})
    message = "; ".join(str(error.get("message", "")) for error in errors)
    error_class = MondayAuthError if _AUTH_ERROR_CODES.intersection(codes) else MondayAPIError
    raise error_class(f"monday.com rejected the query ({', '.join(codes)}): {message}")


def _errors(response) -> list[dict]:
    body = _json(response)
    errors = body.get("errors") or []
    # Some errors still come in the older shape, a top-level error_code.
    if not errors and body.get("error_code"):
        errors = [
            {"message": body.get("error_message", ""), "extensions": {"code": body["error_code"]}}
        ]
    return [error for error in errors if isinstance(error, dict)]


def _extensions(error: dict) -> dict:
    extensions = error.get("extensions")
    return extensions if isinstance(extensions, dict) else {}


def _json(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _clean_token(token: str | None) -> str:
    """Validate a token without ever echoing it.

    Personal API tokens and OAuth access tokens are both sent as is in the
    ``Authorization`` header.
    """
    cleaned = (token or "").strip()
    if not cleaned:
        raise MondayAuthError("A monday.com API token is required.")
    if not cleaned.isascii() or any(character.isspace() for character in cleaned):
        raise MondayAuthError("The monday.com API token contains spaces or non-ASCII characters.")
    return cleaned
