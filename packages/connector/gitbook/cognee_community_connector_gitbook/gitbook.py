"""GitBook documents with authoritative snapshot and deferred orphan cleanup."""

import os
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("gitbook_connector")
GITBOOK_TABLE_NAME = "gitbook_documents"
GITBOOK_SOURCE_NAME = "gitbook"


class GitBookError(Exception):
    """An API failure prevents an authoritative snapshot."""


class GitBookAuthError(GitBookError):
    """The personal access token was rejected."""


class GitBookResponseError(GitBookError):
    """Malformed JSON, response structure, or pagination."""


def gitbook_source(
    api_token: str | None = None,
    org_id: str | None = None,
    *,
    base_url: str = "https://api.gitbook.com",
    client: Any = None,
    request_interval: float = 0.1,
):
    """Create a document-mode dlt source; supplied HTTP clients remain caller-owned.

    Credentials and organization default to GITBOOK_API_TOKEN and GITBOOK_ORG_ID.
    Omit the organization to discover all organizations accessible to the token.
    Use the same dedicated cognee dataset and pipeline storage for each sync.
    """
    import dlt
    import httpx

    api_token = api_token or os.environ.get("GITBOOK_API_TOKEN")
    org_id = org_id or os.environ.get("GITBOOK_ORG_ID")
    if not api_token:
        raise ValueError("Pass api_token= or set GITBOOK_API_TOKEN.")
    base_url = base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError("GitBook base_url must be an HTTP(S) URL.")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("GitBook base_url must not contain credentials, query, or fragment.")
    if request_interval < 0:
        raise ValueError("request_interval must be nonnegative.")

    @dlt.resource(name=GITBOOK_TABLE_NAME, primary_key="id", write_disposition="replace")
    def gitbook_documents():
        session = client if client is not None else httpx.Client(timeout=30)
        try:
            api = _API(session, base_url, api_token, request_interval)
            # Materialize everything before yielding: failures must never replace
            # staging with a partial snapshot or trigger erroneous orphan cleanup.
            yield _sync(api, org_id)
        finally:
            if client is None:
                session.close()

    @dlt.source(name=GITBOOK_SOURCE_NAME)
    def _gitbook():
        return gitbook_documents

    source = _gitbook()
    setattr(source, DOCUMENT_SOURCE_ATTR, GITBOOK_SOURCE_NAME)
    return source


def _object(value):
    if not isinstance(value, dict):
        raise GitBookResponseError("Expected a GitBook JSON object; refusing partial snapshot.")
    return value


def _text(item, key):
    value = _object(item).get(key)
    if not isinstance(value, str):
        raise GitBookResponseError(f"Expected GitBook string field {key}.")
    return value


def _items(item, key):
    value = _object(item).get(key)
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise GitBookResponseError(f"Expected GitBook object array {key}.")
    return value


def _id(item):
    value = _text(item, "id")
    if not value:
        raise GitBookResponseError("Empty GitBook entity ID.")
    return quote(value, safe="")


class _API:
    """Sequential reads with one Retry-After retry and origin-safe pagination."""

    def __init__(self, client, base_url, token, interval=0):
        self.client = client
        self.base_url = base_url
        self.interval = interval
        self.headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    def request(self, path):
        import httpx

        url = self.base_url + path
        for attempt in range(2):
            time.sleep(self.interval)
            try:
                response = self.client.get(url, headers=self.headers, follow_redirects=False)
            except httpx.TransportError:
                raise GitBookError("GitBook transport failure; snapshot aborted.") from None
            if response.status_code == 401:
                raise GitBookAuthError(
                    "GitBook authentication failed (401); check GITBOOK_API_TOKEN."
                )
            if response.status_code == 404:
                logger.warning("GitBook: resource not found (404); skipping.")
                return None, None
            if response.status_code == 429 and attempt == 0:
                raw = response.headers.get("Retry-After", "1")
                try:
                    delay = float(raw)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        delay = 1
                time.sleep(max(0, delay))
                continue
            if not 200 <= response.status_code < 300:
                raise GitBookError(f"GitBook request failed (HTTP {response.status_code}).")
            try:
                payload = response.json()
            except ValueError:
                raise GitBookResponseError("GitBook returned malformed JSON.") from None
            return _object(payload), response.links.get("next", {}).get("url")
        raise GitBookError("GitBook retry exhausted.")  # pragma: no cover

    def get(self, path):
        return self.request(path)[0]

    def listing(self, path):
        seen = set()
        while path:
            if path in seen:
                raise GitBookResponseError("GitBook pagination did not advance.")
            seen.add(path)
            payload, link = self.request(path)
            if payload is None:
                return
            yield from _items(payload, "items")
            if payload.get("next") is not None:
                cursor = _text(payload["next"], "page")
                if not cursor:
                    raise GitBookResponseError("Empty GitBook pagination cursor.")
                parsed = urlsplit(path)
                params = dict(parse_qsl(parsed.query)) | {"page": cursor}
                path = urlunsplit(("", "", parsed.path, urlencode(params), ""))
            elif link:
                target = urlsplit(urljoin(self.base_url + path, link))
                current = urlsplit(self.base_url + path)
                # Never forward bearer credentials to another origin or accept
                # token query parameters supplied by a server pagination link.
                if (
                    (target.scheme, target.netloc, target.path)
                    != (current.scheme, current.netloc, current.path)
                    or target.fragment
                    or any(k not in {"page", "all", "limit"} for k, _ in parse_qsl(target.query))
                ):
                    raise GitBookResponseError("Unsafe GitBook pagination link.")
                path = urlunsplit(("", "", urlsplit(path).path, target.query, ""))
            else:
                return


def _sync(api, org_id):
    rows = {}
    orgs = [{"id": org_id}] if org_id else api.listing("/v1/orgs")
    for org in orgs:
        org_key = _id(org)
        for site in api.listing(f"/v1/orgs/{org_key}/sites"):
            site_key = _id(site)
            site_title = _text(site, "title")
            urls = _object(site.get("urls"))
            url = urls.get("published") or _text(urls, "app")
            visibility = _text(site, "visibility")
            key = f"site:{org_key}:{site_key}"
            rows[key] = {
                "id": key,
                "title": site_title,
                "url": url,
                "content": (
                    f"# {site_title}\n\nType: site\nOrganization: {org['id']}\n"
                    f"URL: {url}\nVisibility: {visibility}"
                ),
            }
            spaces = api.listing(f"/v1/orgs/{org_key}/sites/{site_key}/site-spaces")
            seen_spaces = set()
            for site_space in spaces:
                space_key = _id(_object(site_space.get("space")))
                if space_key in seen_spaces:
                    continue
                seen_spaces.add(space_key)
                space = api.get(f"/v1/spaces/{space_key}")
                if space is None:
                    continue
                space_title = _text(space, "title")
                revision = api.get(f"/v1/spaces/{space_key}/content")
                if revision is None:
                    continue
                for page, parent in _pages(_items(revision, "pages")):
                    page_key = _id(page)
                    kind = _text(page, "type")
                    if kind not in {"document", "group", "link", "computed"}:
                        raise GitBookResponseError("Unknown GitBook page type.")
                    detail = page
                    body = ""
                    if kind == "document":
                        detail = api.get(
                            f"/v1/spaces/{space_key}/content/page/{page_key}?format=markdown"
                        )
                        if detail is None:
                            continue
                        if "markdown" in detail:
                            body = _text(detail, "markdown")
                        elif detail.get("documentId") or "document" in detail:
                            raise GitBookResponseError(
                                "GitBook page is missing requested markdown."
                            )
                    title = _text(detail, "title")
                    path = _text(detail, "path") if kind in {"document", "group"} else ""
                    if kind == "link":
                        body = str(page.get("href", ""))
                    key = f"page:{org_key}:{site_key}:{space_key}:{page_key}"
                    rows[key] = {
                        "id": key,
                        "title": title,
                        "url": f"{api.base_url}/v1/spaces/{space_key}/content/page/{page_key}",
                        "content": (
                            f"# {title}\n\nPath: {path}\nParent page: {parent or ''}\n"
                            f"Space: {space_title}\nSite: {site_title}\n"
                            f"Organization: {org['id']}\n\n{body}"
                        ),
                    }
    # cognee 1.4.0 skips orphan cleanup on completely empty snapshots.
    manifest = {
        "id": "gitbook:source",
        "url": api.base_url,
        "title": "GitBook source",
        "content": f"GitBook knowledge source: {api.base_url}",
    }
    logger.info("GitBook: synced %d document(s).", len(rows))
    return [manifest, *(rows[key] for key in sorted(rows))]


def _pages(pages, parent=None):
    for page in pages:
        page_id = _text(page, "id")
        yield page, parent
        children = (
            _items(page, "pages")
            if page.get("type") in {"document", "group"} or "pages" in page
            else []
        )
        yield from _pages(children, page_id)
