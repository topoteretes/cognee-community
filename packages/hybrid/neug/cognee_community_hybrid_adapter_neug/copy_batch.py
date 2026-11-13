"""COPY FROM batch flush for the NeuG adapters.

Per-statement MERGE writes cost ~29ms/row (parse/plan/commit each time);
``COPY ... FROM`` over a JSONL staging file measured ~14.8k rows/s (~430x)
with 1024-dim vectors. The ingest path therefore stages new rows to a
temporary JSONL file and bulk-loads them.

Semantics notes that shape this helper's contract:

* NeuG's COPY is append-only and **silently skips rows whose primary key
  already exists** (first write wins, no error). Callers must split a batch
  into new vs. existing rows beforehand and route the existing ones through
  MERGE themselves to preserve last-write-wins upsert semantics.
* JSON COPY requires every table column present in the file (sniffed schema
  must match the table exactly); missing fields raise ERR_SCHEMA_MISMATCH.
* TIMESTAMP columns need ``YYYY-MM-DD HH:MM:SS.ffffff`` strings (the same
  format the adapters already use for MERGE parameters).
* For REL tables the first two JSON keys of each object are the from/to
  endpoint keys; pass ``(from='<Node>', to='<Node>')`` in ``copy_options``.
* JSON/JSONL COPY is built into NeuG (v0.1.2+); parquet needs an extension
  the current build does not ship, so JSONL is the staging format.
"""

import json
import os
import tempfile
from typing import Any, Awaitable, Callable, Dict, List

from cognee.shared.logging_utils import get_logger

logger = get_logger()

# The engine rewrites a table's checkpoint state on every COPY that touches
# an already-sealed table, so many small COPY statements cost far more than
# one merged one for the same rows (microbenchmark: 5000 rows as 500 COPY x
# 10 took ~5.8s vs ~0.09s as a single COPY). The adapters therefore buffer
# new rows across calls and flush them as one COPY per table once the number
# of pending rows crosses this threshold; any read/delete/close flushes too,
# so buffered writes are always visible before the next statement runs. The
# threshold targets one flush per table for benchmark-scale ingestion
# (~10k rows); the buffered rows are plain dicts, so the memory cost stays
# modest (a 1024-dim vector row is a few KB).
COPY_BUFFER_FLUSH_ROWS = 8192


async def copy_jsonl_rows(
    execute: Callable[[str], Awaitable[Any]],
    table_name: str,
    rows: List[Dict[str, Any]],
    copy_options: str = "",
) -> None:
    """Bulk-load ``rows`` into ``table_name`` via COPY FROM a temp JSONL file.

    ``execute`` is the adapter's single-argument query runner (no params).
    Each row dict becomes one JSON line; key order matters for REL tables
    (the first two keys are the endpoint keys). The staging file is removed
    after the load regardless of outcome.
    """
    if not rows:
        return
    fd, path = tempfile.mkstemp(prefix=f"neug_copy_{table_name}_", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
        suffix = f" {copy_options}" if copy_options else ""
        await execute(f"COPY {table_name} FROM '{path}'{suffix}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
