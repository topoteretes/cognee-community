"""Microsoft Graph client config for dlt's REST API toolkit.

Every Microsoft 365 connector (Teams, SharePoint, Outlook) talks to the same
API with the same Entra ID app registration, so the auth, paging and retry
setup lives here once. ``graph_client_config`` returns the ``client`` block of
a ``rest_api`` ``RESTAPIConfig``; ``graph_rest_client`` builds a ``RESTClient``
from the same block for the walks that need to stop early, which the
declarative resources cannot express.

* **Auth**: OAuth2 client credentials (app-only) against the tenant's v2.0
  token endpoint with the ``https://graph.microsoft.com/.default`` scope, so
  the token carries whatever application permissions an admin consented to.
  dlt's ``OAuth2ClientCredentials`` caches the token and refreshes it on
  expiry.
* **Paging**: Graph returns the next page as an absolute ``@odata.nextLink``
  URL in the body. ``JSONLinkPaginator`` follows it and drops the original
  query params, which the link already carries.
* **Retries**: dlt's default HTTP session retries 429 and 5xx responses and
  honours Graph's ``Retry-After`` header, so throttling is handled without
  extra code.
"""

import os
from typing import Any

from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.auth import OAuth2ClientCredentials
from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0/"
# ".default" asks for every application permission granted to the app.
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
# The key holds a dot, so it has to be bracket-quoted for dlt's JSONPath.
GRAPH_NEXT_LINK_PATH = "['@odata.nextLink']"
# Collections come back as {"value": [...], "@odata.nextLink": "..."}.
GRAPH_DATA_SELECTOR = "value"

# One set of variables for the shared app registration, so the Teams,
# SharePoint and Outlook connectors can all read the same environment.
TENANT_ID_ENV = "MS_GRAPH_TENANT_ID"
CLIENT_ID_ENV = "MS_GRAPH_CLIENT_ID"
CLIENT_SECRET_ENV = "MS_GRAPH_CLIENT_SECRET"

_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def graph_credentials(
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[str, str, str]:
    """Resolve ``(tenant_id, client_id, client_secret)``, falling back to ``MS_GRAPH_*``."""
    tenant_id = tenant_id or os.environ.get(TENANT_ID_ENV)
    client_id = client_id or os.environ.get(CLIENT_ID_ENV)
    client_secret = client_secret or os.environ.get(CLIENT_SECRET_ENV)
    missing = [
        name
        for name, value in (
            (TENANT_ID_ENV, tenant_id),
            (CLIENT_ID_ENV, client_id),
            (CLIENT_SECRET_ENV, client_secret),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Microsoft Graph app credentials required: pass tenant_id/client_id/"
            f"client_secret or set {', '.join(missing)}."
        )
    return tenant_id, client_id, client_secret


def graph_client_config(
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    *,
    session: Any = None,
) -> dict[str, Any]:
    """Return the ``rest_api`` client block for app-only Microsoft Graph access.

    Args:
        tenant_id: Directory (tenant) id of the Entra ID app registration.
            Falls back to ``MS_GRAPH_TENANT_ID``.
        client_id: Application (client) id. Falls back to ``MS_GRAPH_CLIENT_ID``.
        client_secret: Client secret value. Falls back to
            ``MS_GRAPH_CLIENT_SECRET``.
        session: ``requests`` session used for both the token request and the
            Graph calls (mainly a test-injection point). When omitted, dlt's
            retrying session is used.

    Returns:
        A dict usable as ``RESTAPIConfig["client"]`` or with
        :func:`graph_rest_client`.
    """
    tenant_id, client_id, client_secret = graph_credentials(tenant_id, client_id, client_secret)

    auth = OAuth2ClientCredentials(
        access_token_url=_TOKEN_URL.format(tenant_id=tenant_id),
        client_id=client_id,
        client_secret=client_secret,
        access_token_request_data={"scope": GRAPH_DEFAULT_SCOPE},
        session=session,
    )
    config: dict[str, Any] = {
        "base_url": GRAPH_BASE_URL,
        "auth": auth,
        "paginator": JSONLinkPaginator(next_url_path=GRAPH_NEXT_LINK_PATH),
    }
    if session is not None:
        config["session"] = session
    return config


def graph_rest_client(client_config: dict[str, Any]) -> RESTClient:
    """Build a ``RESTClient`` from a :func:`graph_client_config` block."""
    return RESTClient(
        base_url=client_config["base_url"],
        headers=client_config.get("headers"),
        auth=client_config.get("auth"),
        paginator=client_config.get("paginator"),
        data_selector=GRAPH_DATA_SELECTOR,
        session=client_config.get("session"),
    )
