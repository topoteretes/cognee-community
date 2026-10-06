"""DLT source for Snyk issues (full-snapshot sync + forget-on-delete).

Fetches an organization's issues from Snyk's REST API and yields them as a
dlt resource for cognee's ingestion pipeline.

Like the relational dlt path, each issue is ingested as a *normal document*:
the source declares ``cognee_document_source = "snyk"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "snyk"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each issue
flows through the standard cognify entity-extraction pipeline — the right
treatment for prose such as descriptions and remediation advice — instead of
the deterministic dlt-row schema-context path.

The source is a full snapshot: ``write_disposition="replace"`` rewrites
staging with exactly the issues currently visible to the token each run.
Fixed or deleted issues simply drop out of Snyk's listings, so they are
absent from the snapshot and cognee's existing ``orphan_cleanup`` removes
them from the graph and vector stores. Unchanged issues keep a stable
content-hash ``data_id``, so they are not re-ingested or re-cognified.
(Snyk has no delete feed, so a merge + ``hard_delete`` approach cannot see
removals — hence the full-snapshot model.)

Deliberately, there is no ``introduced_since`` filter: under ``replace``, a
partial result set is indistinguishable from mass remediation — it would
forget live issues. Recency scoping, if ever needed, must be server-side and
complete within its scope.

Findings duplicate heavily across projects sharing a dependency, so issues
sharing a CVE are deduplicated to their first occurrence before yield.
"""

import os
import time
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("snyk_connector")

# dlt resource / staging-table name for Snyk issues.
SNYK_TABLE_NAME = "snyk_issues"
SNYK_SOURCE_NAME = "snyk"
# Pin the Snyk REST API version so upstream changes can't silently alter parsing.
_API_VERSION = "2024-10-15"
_DEFAULT_BASE_URL = "https://api.snyk.io/rest"

# Retry budget for rate-limited / transient Snyk API responses.
_MAX_RETRIES = 5

_EXTRA_HINT = (
    'The Snyk connector requires the "snyk" extra: pip install "cognee[snyk]" '
    "(provides dlt and httpx)."
)


def snyk_source(
    token: str | None = None,
    org_id: str | None = None,
    base_url: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Snyk issues as documents.

    Args:
        token: Snyk API token. Falls back to ``SNYK_TOKEN``.
        org_id: Snyk organization ID. Falls back to ``SNYK_ORG_ID``.
        base_url: Regional REST base URL (defaults to ``api.snyk.io``;
            tokens are region-specific, so match this to the token's region).
        client: Pre-built ``httpx.Client`` (mainly a test-injection point);
            when omitted one is built from the token above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("SNYK_TOKEN")
    if not resolved_token:
        raise ValueError("Snyk API token required: pass token= or set SNYK_TOKEN.")
    resolved_org = org_id or os.environ.get("SNYK_ORG_ID")
    if not resolved_org:
        raise ValueError("Snyk organization ID required: pass org_id= or set SNYK_ORG_ID.")

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        client = httpx.Client(
            base_url=base_url or _DEFAULT_BASE_URL,
            headers={
                "Authorization": f"token {resolved_token}",
                "Content-Type": "application/vnd.api+json",
            },
            timeout=30.0,
        )

    @dlt.resource(name=SNYK_TABLE_NAME, primary_key="id", write_disposition="replace")
    def snyk_issues():
        # Full-snapshot sync: each run replaces staging with exactly the issues
        # currently visible to the token. Fixed/deleted issues drop out of the
        # listing, so they fall out of staging and cognee's orphan_cleanup then
        # forgets them from the graph + vector stores. Unchanged issues keep a
        # stable content-hash data_id, so they are not re-ingested/re-cognified.
        #
        # A request error is NOT swallowed: because staging is authoritative
        # (replace), issues missing from a partial snapshot would be forgotten
        # as if remediated. Letting the error abort the run leaves staging —
        # and memory — untouched, which is the safe failure. Transient blips
        # are already retried in _request; only a persistent failure reaches
        # here.
        seen: set[str] = set()
        count = 0
        for issue in _paginate(client, resolved_org):
            row = _issue_to_row(issue)
            if not row.get("id"):
                continue
            key = row.pop("_dedupe_key")
            if key in seen:
                continue
            seen.add(key)
            yield row
            count += 1
        logger.info("Snyk: synced %d issue(s).", count)

    @dlt.source(name=SNYK_SOURCE_NAME)
    def _snyk():
        return snyk_issues

    source = _snyk()
    # Opt into the document ingestion path (issue → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, SNYK_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Snyk API helpers (module-private)
# ---------------------------------------------------------------------------


def _request(client, url: str, params: dict | None):
    """GET a Snyk REST URL, retrying rate-limit / transient errors.

    httpx does not retry and Snyk throttles aggressively, so a large
    organization would otherwise 429 and abort the sync. Rate-limit (429),
    server (5xx), timeout, and network errors are retried with backoff,
    honoring Retry-After; permanent errors (auth, not-found) and exhausted
    retries propagate so the caller can decide.
    """
    import httpx

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if attempt == _MAX_RETRIES - 1 or status not in (429, 500, 502, 503, 504):
                raise
            delay = _retry_after(exc.response.headers, attempt)
            logger.warning(
                "Snyk: HTTP %d — retrying in %.1fs (%d/%d).",
                status,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = float(2**attempt)
            logger.warning(
                "Snyk: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def _paginate(client, org_id: str):
    """Yield raw Snyk issues across the cursor-based pagination."""
    seen_urls: set[str] = set()
    url = f"/orgs/{org_id}/issues"
    params: dict | None = {"version": _API_VERSION, "limit": 100}
    while url:
        # The API embeds the follow-up query in links.next; only the first
        # request needs explicitly built params.
        body = _request(client, url, params)
        params = None
        yield from body.get("data", [])
        links = body.get("links") or {}
        url = links.get("next")
        # Stop on the last page, or on a repeated cursor (contract violation)
        # so a malformed response can't loop forever.
        if not url or url in seen_urls:
            return
        seen_urls.add(url)


def _issue_cves(issue: dict) -> list[str]:
    """Collect CVE identifiers for an issue across known attribute shapes."""
    attributes = issue.get("attributes") or {}
    cves: list[str] = []
    for problem in attributes.get("problems") or []:
        if isinstance(problem, dict) and problem.get("id", "").startswith("CVE-"):
            cves.append(problem["id"])
    identifiers = attributes.get("identifiers") or {}
    if isinstance(identifiers, dict):
        for cve in identifiers.get("CVE") or []:
            if isinstance(cve, str) and cve not in cves:
                cves.append(cve)
    return cves


def _issue_url(issue: dict) -> str | None:
    """Best-effort permalink for an issue."""
    attributes = issue.get("attributes") or {}
    if attributes.get("url"):
        return attributes["url"]
    key = attributes.get("key") or ""
    if isinstance(key, str) and key.startswith("SNYK-"):
        return f"https://security.snyk.io/vuln/{key}"
    return None


def _issue_to_row(issue: dict) -> dict:
    """Flatten a Snyk issue into a document row.

    Only ``title``/``content`` (+ ``id``/``url`` for identity and provenance)
    are kept, so a metadata-only change that leaves the finding's text
    untouched does not churn the content-hash data_id. ``_dedupe_key`` is a
    transient helper the resource pops before yield — never staged.
    """
    attributes = issue.get("attributes") or {}
    cves = _issue_cves(issue)
    severity = attributes.get("effectiveSeverityLevel") or attributes.get("severity") or ""
    package = attributes.get("packageName") or attributes.get("package") or ""
    project = attributes.get("projectName") or attributes.get("project") or ""
    introduced = attributes.get("introducedDate") or attributes.get("createdAt") or ""
    title = attributes.get("title") or " ".join(p for p in (severity, package) if p)

    lines: list[str] = []
    if severity:
        lines.append(f"Severity: {severity}")
    if cves:
        lines.append(f"CVE: {', '.join(cves)}")
    if package:
        lines.append(f"Package: {package}")
    if project:
        lines.append(f"Project: {project}")
    if introduced:
        lines.append(f"Introduced: {introduced}")
    description = attributes.get("description") or ""
    if description:
        lines += ["", description]
    remediation = (
        attributes.get("remediation") or attributes.get("advice") or attributes.get("fixInfo")
    )
    if isinstance(remediation, dict):
        remediation = remediation.get("advice") or remediation.get("fixedIn") or ""
    if remediation:
        lines += ["", f"Remediation: {remediation}"]
    content = "\n".join(lines).strip()

    dedupe_key = cves[0] if cves else f"snyk:{issue.get('id')}"
    return {
        "id": issue.get("id"),
        "url": _issue_url(issue),
        "title": title,
        "content": content,
        "_dedupe_key": dedupe_key,
    }
