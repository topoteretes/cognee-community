# syntax=docker/dockerfile:1
#
# Official cognee API image + the community ArcadeDB hybrid adapter.
#
# cognee's server never imports community adapters on its own, so this image
#   1. installs cognee-community-hybrid-adapter-arcadedb into cognee's venv, and
#   2. drops a sitecustomize.py into that venv so every Python process in the
#      container (the migration step in entrypoint.sh and the gunicorn workers)
#      registers the "arcadedb" provider and its dataset database handlers
#      before cognee resolves its configuration.
#
# Build context is the repository root (see docker-compose.yml and the root
# .dockerignore), so the adapter is installed from this checkout. Set
# ADAPTER_SOURCE to a PyPI requirement (e.g.
# "cognee-community-hybrid-adapter-arcadedb==0.5.0") to install a published
# release instead.
#
# The adapter pins an exact cognee version in its pyproject.toml; keep
# COGNEE_VERSION in step with it.

ARG COGNEE_VERSION=1.6.1
FROM cognee/cognee:${COGNEE_VERSION}

ARG ADAPTER_SOURCE=/tmp/adapter-src

USER root

# uv is not shipped in the final cognee image; borrow the static binary.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY packages/hybrid/arcadedb/pyproject.toml packages/hybrid/arcadedb/README.md /tmp/adapter-src/
COPY packages/hybrid/arcadedb/cognee_community_hybrid_adapter_arcadedb /tmp/adapter-src/cognee_community_hybrid_adapter_arcadedb

# --no-deps: cognee, aiohttp and the neo4j (Bolt) driver are already in the
# image at the versions cognee was tested with; re-resolving them could
# silently move litellm/instructor off cognee's lockfile.
RUN uv pip install --python /app/.venv/bin/python --no-cache --no-deps "${ADAPTER_SOURCE}" \
    && /app/.venv/bin/python -c "import aiohttp, neo4j, cognee_community_hybrid_adapter_arcadedb" \
    && rm -rf /tmp/adapter-src /usr/local/bin/uv

# Install the auto-register shim into the venv's site-packages.
COPY --chown=cognee:cognee deploy/sitecustomize.py /tmp/sitecustomize.py
RUN site_packages="$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && mv /tmp/sitecustomize.py "${site_packages}/sitecustomize.py"

USER cognee

# Fail the build, not the first request, if registration cannot run.
RUN cd /tmp && python -c "\
from cognee.infrastructure.databases.graph.supported_databases import supported_databases as g; \
from cognee.infrastructure.databases.vector.supported_databases import supported_databases as v; \
assert 'arcadedb' in g and 'arcadedb' in v, (list(g), list(v)); \
print('arcadedb adapter registered')"
