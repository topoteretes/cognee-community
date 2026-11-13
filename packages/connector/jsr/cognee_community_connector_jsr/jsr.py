"""JSR (JavaScript Registry) data-source connector for Cognee."""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Generator
from typing import Any
from urllib.parse import urlparse

import dlt
import requests
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger("jsr_connector")

DEFAULT_BASE_URL = "https://api.jsr.io"
DEFAULT_TIMEOUT = (10, 30)
DEFAULT_RETRIES = 3


class JSRSourceError(RuntimeError):
    """Base error raised by the JSR connector."""


class JSRAPIError(JSRSourceError):
    """JSR API returned an unexpected error or could not be reached."""


class JSRNotFoundError(JSRAPIError):
    """The requested scope, package, or version was not found (HTTP 404)."""


class JSRRateLimitedError(JSRSourceError):
    """JSR answered with HTTP 429 rate limit exceeded."""


def _clean_path(path: str) -> str:
    """Strip variable identifiers for safe error reporting."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2:
        return f"/{parts[0]}/{{param}}"
    return path


def _normalize_package_id(raw: str) -> tuple[str, str]:
    """Normalize a scope and package name from formats like '@std/path' or 'std/path'."""
    cleaned = raw.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    parts = cleaned.split("/", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            f"Invalid JSR package identifier {raw!r}. Expected '@scope/package' or 'scope/package'."
        )
    return parts[0].strip(), parts[1].strip()


def _normalize_scope_name(raw: str) -> str:
    """Normalize a scope name from '@std' or 'std'."""
    cleaned = raw.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    if not cleaned:
        raise ValueError(f"Invalid JSR scope identifier {raw!r}.")
    return cleaned


class JSRClient:
    """Synchronous REST client for JSR (JavaScript Registry) API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("The JSR base_url cannot be empty")
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid JSR base_url: {base_url!r}")

        self.base_url = base
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"JSRClient(base_url={self.base_url!r})"

    def request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Cognee-Connector-JSR/0.1.0",
        }

        for attempt in range(DEFAULT_RETRIES):
            last = attempt == DEFAULT_RETRIES - 1
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=DEFAULT_TIMEOUT,
                )
            except requests.RequestException as exc:
                if last:
                    raise JSRAPIError(
                        f"JSR request failed on {_clean_path(path)}: {type(exc).__name__}"
                    ) from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, path)

    def _parse(self, response: requests.Response, path: str) -> Any:
        status = response.status_code
        if status == 404:
            raise JSRNotFoundError(f"JSR resource not found (HTTP 404): {_clean_path(path)}")
        if status == 429:
            raise JSRRateLimitedError("JSR rate limit exceeded (HTTP 429)")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status != 200:
            raise JSRAPIError(f"JSR request failed with HTTP {status}: {_clean_path(path)}")

        if not isinstance(payload, (list, dict)):
            raise JSRAPIError(f"JSR returned an unexpected response format for {_clean_path(path)}")

        return payload

    def list_scope_packages(self, scope: str) -> list[dict[str, Any]]:
        """List all package definitions under a specific organization scope."""
        res = self.request(f"/scopes/{scope}/packages")
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and "items" in res and isinstance(res["items"], list):
            return res["items"]
        return []

    def get_package(self, scope: str, name: str) -> dict[str, Any]:
        """Fetch package metadata and latest version release info."""
        res = self.request(f"/scopes/{scope}/packages/{name}")
        if not isinstance(res, dict):
            raise JSRAPIError(f"Malformed package response for @{scope}/{name}")
        return res

    def get_package_version(self, scope: str, name: str, version: str) -> dict[str, Any]:
        """Fetch version manifest including exports and documentation."""
        res = self.request(f"/scopes/{scope}/packages/{name}/{version}")
        if not isinstance(res, dict):
            raise JSRAPIError(f"Malformed version response for @{scope}/{name}@{version}")
        return res


def _format_package_content(
    scope: str,
    name: str,
    latest_version: str,
    description: str,
    runtime_compat: dict[str, Any],
    repo_url: str,
    exports: dict[str, Any],
    readme_text: str,
) -> str:
    """Format package overview and technical specs into Markdown for Cognee."""
    compat_list = [k for k, v in runtime_compat.items() if v] if runtime_compat else []
    compat_str = ", ".join(sorted(compat_list)) if compat_list else "Universal (all runtimes)"
    repo_display = repo_url or "None configured"

    exports_lines = []
    if exports and isinstance(exports, dict):
        for k, v in sorted(exports.items()):
            exports_lines.append(f"- `{k}` -> `{v}`")
    exports_str = "\n".join(exports_lines) if exports_lines else "Standard default export"

    lines = [
        f"# JSR Package: @{scope}/{name}",
        f"**Latest Version:** {latest_version}",
        f"**Description:** {description or 'No description provided.'}",
        f"**Runtime Compatibility:** {compat_str}",
        f"**Repository:** {repo_display}",
        "",
        "## Exports",
        exports_str,
    ]

    if readme_text:
        lines.extend(["", "## Documentation Overview", readme_text.strip()])

    return "\n".join(lines)


def _iter_jsr_rows(
    client: JSRClient,
    scopes: list[str],
    package_tuples: list[tuple[str, str]],
    include_docs: bool,
    previous_state: dict[str, str],
) -> Generator[dict[str, Any], None, None]:
    """Iterate JSR package documents, computing hashes and detecting deletions."""
    all_targets: dict[tuple[str, str], dict[str, Any] | None] = {}

    for scope in scopes:
        try:
            scope_pkgs = client.list_scope_packages(scope)
            for pkg in scope_pkgs:
                name = pkg.get("name")
                if name:
                    all_targets[(scope, name)] = pkg
        except JSRNotFoundError:
            logger.warning("JSR scope @%s was not found", scope)

    for scope, name in package_tuples:
        if (scope, name) not in all_targets:
            all_targets[(scope, name)] = None

    current_ids: set[str] = set()

    for (scope, name), _pkg_info in sorted(all_targets.items()):
        package_id = f"jsr:@{scope}/{name}"
        current_ids.add(package_id)

        try:
            full_pkg = client.get_package(scope, name)
        except JSRNotFoundError:
            continue

        latest_version = full_pkg.get("latestVersion", "latest")
        description = full_pkg.get("description", "")
        runtime_compat = full_pkg.get("runtimeCompat", {})
        github_repo = full_pkg.get("githubRepository", {})
        repo_url = ""
        if isinstance(github_repo, dict) and github_repo.get("owner") and github_repo.get("name"):
            repo_url = f"https://github.com/{github_repo['owner']}/{github_repo['name']}"

        exports: dict[str, Any] = {}
        readme_text = ""
        if include_docs and latest_version and latest_version != "latest":
            try:
                version_info = client.get_package_version(scope, name, latest_version)
                exports = version_info.get("exports", {})
                readme_text = version_info.get("readme", "")
            except (JSRAPIError, JSRNotFoundError):
                pass

        content = _format_package_content(
            scope=scope,
            name=name,
            latest_version=latest_version,
            description=description,
            runtime_compat=runtime_compat,
            repo_url=repo_url,
            exports=exports,
            readme_text=readme_text,
        )

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Check incremental state
        if previous_state.get(package_id) == content_hash:
            continue

        previous_state[package_id] = content_hash

        yield {
            "id": package_id,
            "scope": f"@{scope}",
            "package_name": f"@{scope}/{name}",
            "name": name,
            "latest_version": latest_version,
            "description": description,
            "runtime_compat": json.dumps(runtime_compat, sort_keys=True),
            "repository": repo_url,
            "title": f"JSR Package: @{scope}/{name} v{latest_version}",
            "content": content,
            "url": f"https://jsr.io/@{scope}/{name}",
            "updated_at": full_pkg.get("updatedAt", ""),
            "_deleted": False,
        }

    # Emit tombstones for dropped or deleted packages
    dropped_ids = set(previous_state.keys()) - current_ids
    for dropped_id in sorted(dropped_ids):
        del previous_state[dropped_id]
        yield {
            "id": dropped_id,
            "scope": "",
            "package_name": "",
            "name": "",
            "latest_version": "",
            "description": "",
            "runtime_compat": "{}",
            "repository": "",
            "title": f"Deleted: {dropped_id}",
            "content": "",
            "url": "",
            "updated_at": "",
            "_deleted": True,
        }


def jsr_source(
    scopes: list[str] | tuple[str, ...] | None = None,
    packages: list[str] | tuple[str, ...] | None = None,
    include_docs: bool = True,
    base_url: str = DEFAULT_BASE_URL,
    client: JSRClient | None = None,
    service: JSRClient | None = None,
) -> Any:
    """Create a DLT data-source for ingesting JSR packages into Cognee memory."""
    active_client = client or service
    if active_client is None:
        active_client = JSRClient(base_url=base_url)

    normalized_scopes: list[str] = []
    if scopes:
        normalized_scopes = [_normalize_scope_name(s) for s in scopes]

    normalized_packages: list[tuple[str, str]] = []
    if packages:
        normalized_packages = [_normalize_package_id(p) for p in packages]

    if not normalized_scopes and not normalized_packages:
        raise ValueError(
            "At least one JSR scope (e.g. '@std') or package (e.g. '@std/path') must be specified"
        )

    def _source() -> Any:
        @dlt.resource(
            name="jsr_packages",
            write_disposition="merge",
            primary_key="id",
        )
        def jsr_resource() -> Generator[dict[str, Any], None, None]:
            state = dlt.current.resource_state().setdefault("packages", {})
            yield from _iter_jsr_rows(
                active_client,
                scopes=normalized_scopes,
                package_tuples=normalized_packages,
                include_docs=include_docs,
                previous_state=state,
            )

        setattr(jsr_resource, DOCUMENT_SOURCE_ATTR, "jsr")
        return jsr_resource

    return dlt.source(_source, name="jsr")()
