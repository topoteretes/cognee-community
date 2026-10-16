"""Auto-register the ArcadeDB hybrid adapter with cognee in every Python process.

cognee's API server (``gunicorn ... cognee.api.client:app``) and the migration
step in its ``entrypoint.sh`` never import community adapters. Python imports
``sitecustomize`` at interpreter start-up, so installing this file into the
venv's site-packages (see Dockerfile) guarantees that
``cognee_community_hybrid_adapter_arcadedb.register`` has run before cognee
looks up the ``arcadedb`` graph/vector provider or the
``arcadedb_graph_local`` / ``arcadedb_vector_local`` dataset database handlers.

Set ``COGNEE_ARCADEDB_AUTOREGISTER=false`` to skip it for one-off commands.
"""

import os

if os.environ.get("COGNEE_ARCADEDB_AUTOREGISTER", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}:
    import cognee_community_hybrid_adapter_arcadedb.register  # noqa: F401
