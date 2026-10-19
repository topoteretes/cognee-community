"""Local durable admission attempt marker; excludes reuse after an early DLT crash.

Single-host filesystem fence, NOT distributed locking, and not Cognee's native
data-level transaction. A marker is never automatically deleted or reused.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .source import SHA40, SourceError


def claim_attempt(
    root: Path, dataset: str, tag: str, commit: str, selected: tuple[str, ...]
) -> Path:
    if not isinstance(dataset, str) or not dataset or len(dataset) > 160:
        raise SourceError("Invalid attempt dataset")
    if not isinstance(tag, str) or not tag:
        raise SourceError("Invalid attempt tag")
    if not SHA40.fullmatch(commit) or not selected or len(selected) != len(set(selected)):
        raise SourceError("Invalid pinned attempt source or selected identities")
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Dataset name is hashed so path separators / Unicode cannot escape root.
    path = root / (hashlib.sha256(dataset.encode("utf8")).hexdigest() + ".json")
    receipt = {
        "schema": "opensiro-vsm-stage-attempt/v1",
        "state": "CLAIMED_NEVER_REUSABLE",
        "dataset": dataset,
        "tag": tag,
        "commit": commit,
        "selected_count": len(selected),
        "selected_sha256": hashlib.sha256(
            json.dumps(sorted(selected), separators=(",", ":")).encode("utf8")
        ).hexdigest(),
    }
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as ex:
        raise SourceError(
            "Stage dataset was previously claimed; refuse reuse even if no Cognee Data exists"
        ) from ex
    try:
        with os.fdopen(descriptor, "w", encoding="utf8") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        directory = os.open(str(root), os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return path
