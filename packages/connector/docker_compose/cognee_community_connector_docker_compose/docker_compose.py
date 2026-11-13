"""Docker Compose infrastructure data-source connector for cognee.

Parses ``docker-compose.yml`` (and compose override) files into structured service
topologies (images, ports, environment variables, dependencies, volumes, networks,
and health checks) ready for :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_docker_compose import docker_compose_source

    await cognee.remember(
        docker_compose_source("/path/to/docker-compose.yml"),
        dataset_name="infra_topology",
        max_rows_per_table=0,  # ingest full topology snapshot
    )

Design
------
* **Primary key** — ``id = "docker_compose:service:{service_name}"``. Stable across
  re-syncs so incremental updates only re-cognify changed services.
* **Snapshot sync (``replace``)** — Multi-container setups represent current desired state.
  Using ``write_disposition="replace"`` ensures that any service removed from
  ``docker-compose.yml`` is automatically purged from the graph and vector index via
  cognee's orphan cleanup.
* **Agent-Optimized Topology** — Inter-service dependencies (``depends_on``), port mappings,
  volume mounts, and environment configurations are structured so AI DevOps agents
  can accurately query architectures without hallucinations.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("docker_compose_connector")
except ImportError:
    import logging
    logger = logging.getLogger("docker_compose_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

DOCKER_COMPOSE_TABLE_NAME = "docker_compose_services"
DOCKER_COMPOSE_SOURCE_NAME = "docker_compose"


def _format_ports(ports_raw: Any) -> list[str]:
    """Normalize Docker port mappings into readable strings."""
    result: list[str] = []
    if isinstance(ports_raw, list):
        for p in ports_raw:
            if isinstance(p, dict):
                target = p.get("target")
                published = p.get("published")
                protocol = p.get("protocol", "tcp")
                if target and published:
                    result.append(f"{published}:{target}/{protocol}")
                elif target:
                    result.append(f"{target}/{protocol}")
            elif isinstance(p, (str, int)):
                result.append(str(p))
    return result


def _format_environment(env_raw: Any) -> list[str]:
    """Normalize environment variables into key-value strings."""
    result: list[str] = []
    if isinstance(env_raw, dict):
        for k, v in env_raw.items():
            result.append(f"{k}={v if v is not None else ''}")
    elif isinstance(env_raw, list):
        for item in env_raw:
            if isinstance(item, str):
                result.append(item)
    return result


def _format_dependencies(depends_on_raw: Any) -> list[str]:
    """Normalize depends_on definitions."""
    result: list[str] = []
    if isinstance(depends_on_raw, list):
        for dep in depends_on_raw:
            if isinstance(dep, str):
                result.append(dep)
    elif isinstance(depends_on_raw, dict):
        for dep, config in depends_on_raw.items():
            if isinstance(config, dict) and "condition" in config:
                result.append(f"{dep} (condition: {config['condition']})")
            else:
                result.append(str(dep))
    return result


def _format_volumes(volumes_raw: Any) -> list[str]:
    """Normalize volume mounts."""
    result: list[str] = []
    if isinstance(volumes_raw, list):
        for v in volumes_raw:
            if isinstance(v, str):
                result.append(v)
            elif isinstance(v, dict):
                src = v.get("source", "")
                tgt = v.get("target", "")
                mode = f":{v['read_only']}" if v.get("read_only") else ""
                result.append(f"{src}:{tgt}{mode}".strip(":"))
    return result


def _format_service_document(
    service_name: str,
    service_config: dict[str, Any],
    compose_file_name: str,
) -> dict[str, Any]:
    """Format a Docker Compose service into a structured memory document."""
    image = service_config.get("image") or ""
    build = service_config.get("build")
    build_info = ""
    if isinstance(build, str):
        build_info = f"Context: {build}"
    elif isinstance(build, dict):
        build_info = f"Context: {build.get('context', '.')}, Dockerfile: {build.get('dockerfile', 'Dockerfile')}"

    container_name = service_config.get("container_name") or service_name
    ports = _format_ports(service_config.get("ports"))
    env_vars = _format_environment(service_config.get("environment"))
    depends_on = _format_dependencies(service_config.get("depends_on"))
    volumes = _format_volumes(service_config.get("volumes"))
    networks = service_config.get("networks")
    restart = service_config.get("restart") or "no"

    lines: list[str] = [
        f"Docker Compose Service: {service_name}",
        f"Compose File: {compose_file_name}",
        f"Container Name: {container_name}",
    ]

    if image:
        lines.append(f"Image: {image}")
    if build_info:
        lines.append(f"Build: {build_info}")

    lines.append(f"Restart Policy: {restart}")

    if ports:
        lines.append("Exposed Port Mappings:")
        for p in ports:
            lines.append(f"  - {p}")

    if depends_on:
        lines.append("Service Dependencies (depends_on):")
        for dep in depends_on:
            lines.append(f"  - {dep}")

    if volumes:
        lines.append("Mounted Volumes:")
        for v in volumes:
            lines.append(f"  - {v}")

    if networks:
        net_list = networks if isinstance(networks, list) else list(networks.keys()) if isinstance(networks, dict) else [str(networks)]
        lines.append(f"Networks: {', '.join(net_list)}")

    if env_vars:
        lines.append("Environment Variables:")
        for e in env_vars:
            lines.append(f"  - {e}")

    healthcheck = service_config.get("healthcheck")
    if isinstance(healthcheck, dict):
        test = healthcheck.get("test")
        test_str = " ".join(test) if isinstance(test, list) else str(test)
        lines.append(f"Healthcheck: {test_str}")

    content_str = "\n".join(lines)
    img_or_build = image if image else (build_info or "custom")
    title = f"[Docker Service] {service_name} ({img_or_build})"
    primary_key = f"docker_compose:service:{service_name.lower()}"

    return {
        "id": primary_key,
        "title": title,
        "content": content_str,
        "service_name": service_name,
        "image": image,
        "ports": ports,
        "depends_on": depends_on,
        "compose_file": compose_file_name,
    }


def iter_docker_compose_services(compose_path: str | os.PathLike) -> Iterator[dict[str, Any]]:
    """Yield parsed Docker Compose services from a compose YAML file or directory."""
    path = Path(compose_path)
    if not path.exists():
        raise FileNotFoundError(f"Docker compose path does not exist: {path}")

    files: list[Path] = []
    if path.is_file():
        files.append(path)
    elif path.is_dir():
        # Look for standard compose filenames
        patterns = ["*docker-compose*.yml", "*docker-compose*.yaml", "*compose*.yml", "*compose*.yaml"]
        for pat in patterns:
            files.extend(sorted(path.glob(f"**/{pat}")))
        # De-duplicate
        files = list(dict.fromkeys(files))

    if not files:
        logger.warning("Docker compose connector: no compose YAML files found in %s", path)
        return

    count = 0
    for f in files:
        try:
            with f.open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Docker compose connector: failed to parse %s: %s", f, exc)
            continue

        if not isinstance(data, dict):
            continue

        services = data.get("services")
        if not isinstance(services, dict):
            continue

        for svc_name, svc_config in services.items():
            if isinstance(svc_config, dict):
                count += 1
                yield _format_service_document(svc_name, svc_config, f.name)

    logger.info("Docker compose connector: successfully parsed %d service(s) from %s", count, path)


def docker_compose_source(compose_path: str | os.PathLike):
    """Return a ``dlt`` resource over Docker Compose services for :func:`cognee.remember`.

    Args:
        compose_path: Path to a ``docker-compose.yml`` file or directory containing compose files.

    Returns:
        A ``dlt`` resource (``docker_compose_services``) with ``write_disposition="replace"``
        and document source marker (``cognee_document_source = "docker_compose"``).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Docker Compose connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    source_path = str(compose_path)

    @dlt.resource(
        name=DOCKER_COMPOSE_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def docker_compose_services():
        yield from iter_docker_compose_services(source_path)

    setattr(docker_compose_services, DOCUMENT_SOURCE_ATTR, DOCKER_COMPOSE_SOURCE_NAME)
    return docker_compose_services
