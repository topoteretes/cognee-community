"""Private durable state; dlt's loaded state is not a successful Cognee sync."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .models import ConfigurationError


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "last_success": None, "attempt": None}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            "Cannot read synchronization journal; do not reset it blindly"
        ) from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ConfigurationError("Unsupported synchronization journal version")
    return value
