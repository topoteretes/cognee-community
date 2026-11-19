"""DLT source for JFrog Artifactory repositories, package artifacts, and CI/CD build provenance.

Fetches JFrog Artifactory repository topologies, artifact metadata with cryptographic checksums,
and CI/CD build provenance records (SBOM dependencies), rendering them into structured
markdown documents for Cognee's ingestion and graph extraction pipeline.

Uses ``write_disposition="replace"`` and ``DOCUMENT_SOURCE_ATTR = "artifactory"`` so deleted or
decommissioned packages and builds automatically drop out of Cognee's memory via orphan cleanup.
"""

from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("artifactory_connector")
except ImportError:
    import logging

    logger = logging.getLogger("artifactory_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_artifactory.client import ArtifactoryClient

ARTIFACTORY_REPOSITORIES_TABLE_NAME = "artifactory_repositories"
ARTIFACTORY_ARTIFACTS_TABLE_NAME = "artifactory_artifacts"
ARTIFACTORY_BUILDS_TABLE_NAME = "artifactory_builds"
ARTIFACTORY_SOURCE_NAME = "artifactory"

_EXTRA_HINT = (
    'The JFrog Artifactory connector requires the "artifactory" dependencies: '
    'pip install "cognee-community-connector-artifactory" (provides dlt and httpx).'
)


def artifactory_source(
    base_url: str | None = None,
    token: str | None = None,
    api_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
    repo_keys: list[str] | None = None,
    repo_type: str | None = None,
    package_type: str | None = None,
    include_artifacts: bool = True,
    include_builds: bool = True,
    build_names: list[str] | None = None,
    artifact_limit_per_repo: int = 100,
    verify_ssl: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding JFrog Artifactory repositories, artifacts, and build provenance.

    Args:
        base_url: Artifactory server base URL (e.g. ``https://company.jfrog.io/artifactory``).
            Falls back to ``ARTIFACTORY_URL`` environment variable.
        token: Artifactory access token.
            Falls back to ``ARTIFACTORY_TOKEN`` environment variable.
        api_key: Optional legacy API key.
            Falls back to ``ARTIFACTORY_API_KEY`` environment variable.
        username: Username for basic auth fallback.
        password: Password for basic auth fallback.
        repo_keys: Restrict ingestion to specific repository keys
            (e.g. ``["docker-local", "libs-release"]``).
        repo_type: Filter by repository type: ``"local"``, ``"remote"``, or ``"virtual"``.
        package_type: Filter by package type: ``"docker"``, ``"maven"``, ``"npm"``, ``"pypi"``, etc.
        include_artifacts: Whether to ingest artifact metadata within repositories (default: True).
        include_builds: Whether to ingest CI/CD build info and SBOM dependency trees
            (default: True).
        build_names: Restrict build ingestion to specific build names.
        artifact_limit_per_repo: Maximum artifacts to ingest per repository (default: 100).
        verify_ssl: Whether to verify SSL certificates (default: True).
        client: Pre-instantiated ``ArtifactoryClient`` (primarily for test dependency injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        client = ArtifactoryClient(
            base_url=base_url,
            token=token,
            api_key=api_key,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
        )

    server_base = client.base_url

    @dlt.resource(
        name=ARTIFACTORY_REPOSITORIES_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def artifactory_repositories():
        repos = client.get_repositories(repo_type=repo_type, package_type=package_type)
        if repo_keys:
            target_keys = set(repo_keys)
            repos = [r for r in repos if r.get("key") in target_keys]

        count = 0
        for r in repos:
            count += 1
            key = r.get("key", "")
            # Optionally enrich with full details
            try:
                details = client.get_repository_details(key)
            except Exception:
                details = r
            yield _repo_to_document(details or r, server_base)
        logger.info("Artifactory: synced %d repository/repositories.", count)

    @dlt.resource(
        name=ARTIFACTORY_ARTIFACTS_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def artifactory_artifacts():
        if not include_artifacts:
            return

        repos = client.get_repositories(repo_type=repo_type, package_type=package_type)
        target_keys = set(repo_keys) if repo_keys else {r.get("key") for r in repos if r.get("key")}

        total_artifacts = 0
        for key in target_keys:
            if not key:
                continue
            try:
                # First try AQL search for fast batch retrieval
                aql = (
                    f'items.find({{"repo":"{key}","type":"file"}}).limit({artifact_limit_per_repo})'
                )
                results = client.execute_aql(aql)
                if results:
                    for item in results:
                        total_artifacts += 1
                        path = item.get("path", "")
                        name = item.get("name", "")
                        full_path = f"{path}/{name}".strip("/")
                        props = client.get_item_properties(key, full_path)
                        yield _artifact_to_document(key, item, props, server_base)
                    continue

                # Fallback to storage folder traversal
                folder_info = client.get_item_info(key)
                children = folder_info.get("children") or []
                for child in children[:artifact_limit_per_repo]:
                    child_uri = child.get("uri", "").lstrip("/")
                    is_folder = child.get("folder", False)
                    if not is_folder and child_uri:
                        total_artifacts += 1
                        try:
                            item_info = client.get_item_info(key, child_uri)
                        except Exception:
                            item_info = {"name": child_uri, "path": "", "size": 0}
                        props = client.get_item_properties(key, child_uri)
                        yield _artifact_to_document(key, item_info, props, server_base)

            except Exception as exc:
                logger.warning("Artifactory: error inspecting artifacts in repo '%s': %s", key, exc)

        logger.info("Artifactory: synced %d artifact(s).", total_artifacts)

    @dlt.resource(
        name=ARTIFACTORY_BUILDS_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def artifactory_builds():
        if not include_builds:
            return

        count = 0
        try:
            build_runs = client.get_build_runs()
            if build_names:
                target_names = set(build_names)
                build_runs = [b for b in build_runs if b.get("build_name") in target_names]

            for b in build_runs:
                b_name = b.get("build_name")
                if not b_name:
                    continue
                # Get the latest runs for this build name
                try:
                    runs = client.get_build_runs(build_name=b_name)
                    # Take up to the 5 most recent runs
                    for run in runs[:5]:
                        b_num = run.get("uri") or run.get("build_number") or ""
                        if b_num:
                            count += 1
                            info = client.get_build_info(b_name, b_num)
                            yield _build_to_document(b_name, b_num, info, server_base)
                except Exception as exc:
                    logger.warning(
                        "Artifactory: could not fetch build info for '%s': %s", b_name, exc
                    )

        except Exception as exc:
            logger.warning("Artifactory: could not fetch build catalog: %s", exc)

        logger.info("Artifactory: synced %d build run(s).", count)

    @dlt.source(name=ARTIFACTORY_SOURCE_NAME)
    def _artifactory():
        resources = [artifactory_repositories]
        if include_artifacts:
            resources.append(artifactory_artifacts)
        if include_builds:
            resources.append(artifactory_builds)
        return resources

    source = _artifactory()
    setattr(source, DOCUMENT_SOURCE_ATTR, ARTIFACTORY_SOURCE_NAME)
    return source


def _repo_to_document(repo: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Convert a JFrog Artifactory repository resource into a structured markdown document."""
    key = repo.get("key", "unknown")
    r_type = repo.get("rclass") or repo.get("type") or "local"
    pkg_type = repo.get("packageType", "generic")
    description = repo.get("description", "No description provided.")
    layout = repo.get("repoLayoutRef", "simple-default")
    notes = repo.get("notes", "")

    lines = [
        f"# JFrog Artifactory Repository: {key}",
        "",
        "## Repository Configuration",
        f"- **Repository Key**: `{key}`",
        f"- **Repository Type**: {r_type.capitalize()} (e.g. local, remote, virtual)",
        f"- **Package Ecosystem**: `{pkg_type}`",
        f"- **Repository Layout**: {layout}",
        "",
        "## Overview",
        f"{description}",
    ]

    if notes:
        lines.extend(
            [
                "",
                "## Maintenance Notes",
                notes,
            ]
        )

    url_path = f"{base_url}/webapp/#/artifacts/browse/tree/General/{key}"

    content = "\n".join(lines)
    return {
        "id": f"artifactory_repo_{key}",
        "url": url_path,
        "title": f"Artifactory Repository: {key} ({pkg_type})",
        "content": content,
    }


def _artifact_to_document(
    repo_key: str,
    item: dict[str, Any],
    properties: dict[str, Any],
    base_url: str,
) -> dict[str, Any]:
    """Convert an artifact binary or package into a structured markdown document with checksums."""
    name = item.get("name") or "unknown"
    path = item.get("path", "")
    full_path = f"{path}/{name}".strip("/")

    size_bytes = item.get("size", 0)
    if isinstance(size_bytes, str) and size_bytes.isdigit():
        size_bytes = int(size_bytes)
    size_kb = f"{size_bytes / 1024:.1f} KB" if size_bytes else "0 KB"

    created = item.get("created", "N/A")
    modified = item.get("modified") or item.get("lastModified") or "N/A"
    checksums = item.get("checksums") or {}
    sha256 = item.get("sha256") or checksums.get("sha256") or "N/A"
    sha1 = item.get("actual_sha1") or checksums.get("sha1") or "N/A"
    md5 = item.get("actual_md5") or checksums.get("md5") or "N/A"

    download_uri = item.get("downloadUri") or f"{base_url}/{repo_key}/{full_path}"

    lines = [
        f"# Artifactory Artifact: {name}",
        "",
        "## Package Coordinates",
        f"- **Repository**: `{repo_key}`",
        f"- **File Name**: `{name}`",
        f"- **Path within Repo**: `{full_path}`",
        f"- **Artifact Size**: {size_kb} ({size_bytes} bytes)",
        f"- **Last Modified**: {modified}",
        f"- **Created At**: {created}",
        "",
        "## Cryptographic Integrity & Checksums",
        f"- **SHA-256**: `{sha256}`",
        f"- **SHA-1**: `{sha1}`",
        f"- **MD5**: `{md5}`",
    ]

    if properties:
        lines.extend(
            [
                "",
                "## Attached Metadata & Build Tags",
            ]
        )
        for p_key, p_val in properties.items():
            val_str = ", ".join(p_val) if isinstance(p_val, list) else str(p_val)
            lines.append(f"- **{p_key}**: `{val_str}`")

    content = "\n".join(lines)
    artifact_id = f"artifactory_art_{repo_key}_{full_path.replace('/', '_')}"

    return {
        "id": artifact_id,
        "url": download_uri,
        "title": f"Artifactory Artifact: {name} in {repo_key}",
        "content": content,
    }


def _build_to_document(
    build_name: str,
    build_number: str,
    build_info: dict[str, Any],
    base_url: str,
) -> dict[str, Any]:
    """Convert build provenance and SBOM dependency records into structured markdown."""
    started = build_info.get("started", "N/A")
    duration = build_info.get("durationMillis", 0)
    agent = build_info.get("agent", {})
    agent_name = agent.get("name", "Unknown CI") if isinstance(agent, dict) else str(agent)
    agent_version = agent.get("version", "") if isinstance(agent, dict) else ""

    vcs = build_info.get("vcs") or []
    vcs_info = vcs[0] if vcs and isinstance(vcs, list) else {}
    vcs_url = vcs_info.get("url", "N/A")
    vcs_revision = vcs_info.get("revision", "N/A")
    vcs_branch = vcs_info.get("branch", "N/A")

    modules = build_info.get("modules") or []

    lines = [
        f"# Artifactory CI/CD Build Run: {build_name} #{build_number}",
        "",
        "## Build Execution Metadata",
        f"- **Build Name**: `{build_name}`",
        f"- **Build Number**: `{build_number}`",
        f"- **Execution Timestamp**: {started}",
        f"- **Duration**: {duration} ms",
        f"- **CI Agent**: {agent_name} {agent_version}".strip(),
        "",
        "## Version Control & Source Provenance",
        f"- **Git Repository**: {vcs_url}",
        f"- **Commit Revision**: `{vcs_revision}`",
        f"- **Source Branch**: `{vcs_branch}`",
    ]

    total_artifacts = 0
    total_deps = 0
    for mod in modules:
        mod_id = mod.get("id", "module")
        artifacts = mod.get("artifacts") or []
        deps = mod.get("dependencies") or []
        total_artifacts += len(artifacts)
        total_deps += len(deps)

        lines.extend(
            [
                "",
                f"### Module: {mod_id}",
                f"- **Published Artifacts Count**: {len(artifacts)}",
                f"- **Declared Dependencies Count (SBOM)**: {len(deps)}",
            ]
        )

        if artifacts:
            lines.append("  - **Published Artifacts**:")
            for a in artifacts[:5]:
                a_name = a.get("name", "artifact")
                a_sha256 = a.get("sha256", "N/A")
                lines.append(f"    - `{a_name}` (SHA-256: `{a_sha256}`)")

        if deps:
            lines.append("  - **Key Dependencies**:")
            for d in deps[:8]:
                d_id = d.get("id", "dep")
                d_sha1 = d.get("sha1", "")
                sha_str = f" [sha1: {d_sha1[:8]}]" if d_sha1 else ""
                lines.append(f"    - `{d_id}`{sha_str}")

    content = "\n".join(lines)
    build_id = f"artifactory_build_{build_name}_{build_number}"
    build_url = f"{base_url}/webapp/#/builds/{build_name}/{build_number}"

    return {
        "id": build_id,
        "url": build_url,
        "title": f"Artifactory Build: {build_name} #{build_number}",
        "content": content,
    }
