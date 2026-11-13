"""Turn JUnit XML files from CI into failure rows."""

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

Row = dict[str, Any]

MAX_BYTES = 10 * 1024 * 1024
MAX_MESSAGE = 500

_SHA_NAMES = {"commit", "sha", "git_sha", "GITHUB_SHA", "CI_COMMIT_SHA"}
_SHA = re.compile(r"[0-9a-f]{7,40}")
_DTD = re.compile(rb"<!(doctype|entity)", re.IGNORECASE)


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:MAX_MESSAGE]
    return ""


def _commit_sha(node: ET.Element) -> str | None:
    for prop in node.findall("properties/property"):
        value = (prop.get("value") or "").lower()
        if prop.get("name") in _SHA_NAMES and _SHA.fullmatch(value):
            return value
    return None


def parse_junit(job: str, data: bytes) -> list[Row]:
    """Parse one JUnit XML document into ``ci_failures`` rows, sorted by id.

    Raises ``ValueError(reason)`` for any input it cannot use, and nothing else.
    """
    if not data.strip():
        raise ValueError("empty file")
    if b"\0" in data:
        raise ValueError("contains null bytes")
    if _DTD.search(data):
        raise ValueError("DOCTYPE or ENTITY not allowed")
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, LookupError, RecursionError, MemoryError) as exc:
        raise ValueError(f"malformed XML: {exc}"[:200]) from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError("not JUnit XML: root is not testsuites or testsuite")
    rows: dict[str, Row] = {}
    # Iterative walk so deeply nested suites cannot blow the stack; the sha is inherited.
    stack: list[tuple[ET.Element, str | None]] = [(root, None)]
    while stack:
        node, inherited = stack.pop()
        if node.tag == "testcase":
            for child in node:
                if child.tag in ("failure", "error"):
                    name = node.get("name") or ""
                    classname = node.get("classname")
                    test_id = f"{classname}::{name}" if classname else name
                    rows.setdefault(
                        f"{job}:{test_id}",
                        {
                            "id": f"{job}:{test_id}",
                            "job": job,
                            "test_id": test_id,
                            "file": node.get("file"),
                            "commit_sha": inherited,
                            "kind": child.tag,
                            "message": _first_line(child.get("message")) or _first_line(child.text),
                        },
                    )
                    break
            continue
        sha = _commit_sha(node) or inherited
        stack += [(c, sha) for c in reversed(node) if c.tag in ("testsuite", "testcase")]
    return [rows[k] for k in sorted(rows)]


def parse_ci_logs(paths: Iterable[str | Path]) -> dict[str, list[Row]]:
    """Return ``{"ci_failures", "skipped"}`` for JUnit XML files and directories of them.

    A directory contributes every ``*.xml`` under it. A missing path raises ``ValueError``;
    a file that cannot be used becomes a ``skipped`` row instead.
    """
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise ValueError(f"no such path: {path}")
        files += sorted(p for p in path.rglob("*.xml") if p.is_file()) if path.is_dir() else [path]
    failures: dict[str, Row] = {}
    skipped: list[Row] = []
    for file in files:
        try:
            if file.stat().st_size > MAX_BYTES:
                raise ValueError(f"too large: over {MAX_BYTES} bytes")
            for row in parse_junit(file.stem, file.read_bytes()):
                failures.setdefault(row["id"], row)
        except (ValueError, OSError) as exc:
            skipped.append({"file": str(file), "reason": str(exc)})
    return {
        "ci_failures": [failures[k] for k in sorted(failures)],
        "skipped": sorted(skipped, key=lambda s: s["file"]),
    }
