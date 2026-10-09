"""Mutation-test the Evernote connector: does the offline suite actually validate?

For each deliberate defect we inject into the connector, run the matching slice of
the suite, and require it to FAIL. A mutation that survives means the suite has a
blind spot in exactly the behaviour the issue's acceptance criteria depend on.

Usage: python mutation_check.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
MODULE = PKG / "cognee_community_connector_evernote" / "evernote.py"
BACKUP = MODULE.with_suffix(".py.bak")

# Reused pytest targets, kept short so the table below stays readable.
UNIT = ["tests/test_evernote.py"]
DLT = ["tests/test_evernote_dlt.py"]
FORGET = ["tests/test_evernote_forget.py"]
INGEST = ["tests/test_evernote_ingestion.py"]

# (label, old, new, pytest args)
MUTATIONS = [
    (
        "forget-on-delete: drop expunged (permanently deleted) notes",
        """        for guid in getattr(chunk, "expungedNotes", None) or []:
            yield _deleted_row(guid)
            known_ids.discard(guid)
            tombstoned += 1""",
        """        for guid in getattr(chunk, "expungedNotes", None) or []:
            known_ids.discard(guid)
            tombstoned += 1""",
        UNIT + DLT + FORGET,
    ),
    (
        "forget-on-delete: ignore the Trash flag (re-ingest trashed notes)",
        '    return bool(getattr(note, "deleted", None)) or getattr(note, "active", None) is False',
        "    return False",
        UNIT + DLT + FORGET,
    ),
    (
        "incremental: always full-scan instead of resuming from the cursor",
        '    after_usn = 0 if full_scan else int(state.get("cursor_usn") or 0)',
        "    after_usn = 0",
        UNIT,
    ),
    (
        "incremental: never treat the first run as a full scan",
        '    full_scan = state.get("cursor_usn") is None or state.get("scope_key") != scope_key',
        "    full_scan = False",
        UNIT,
    ),
    (
        "safety: delete the empty-scan guard (mass forget-on-delete)",
        "        if known_ids and not live_ids:",
        "        if False:",
        UNIT,
    ),
    (
        "scope: ignore notebook_guids (ingest everything)",
        (
            '    if config.notebook_guids and getattr(note, "notebookGuid", None) '
            "not in config.notebook_guids:"
        ),
        "    if False:",
        UNIT + INGEST,
    ),
    (
        "dlt wiring: switch the resource to write_disposition=replace",
        # Anchor on the decorator block, not the bare keyword — the same string
        # appears in the module docstring and would be edited instead.
        (
            '        primary_key="id",\n        write_disposition="merge",\n'
            "        # _deleted is a boolean"
        ),
        (
            '        primary_key="id",\n        write_disposition="replace",\n'
            "        # _deleted is a boolean"
        ),
        UNIT + DLT,
    ),
    (
        "dlt wiring: drop the hard_delete marker on _deleted",
        '        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},',
        "        columns={},",
        UNIT + DLT,
    ),
    (
        "routing: stop declaring document mode",
        "    setattr(resource, DOCUMENT_SOURCE_ATTR, EVERNOTE_SOURCE_NAME)",
        "    pass",
        UNIT + INGEST,
    ),
    (
        "render: stop flattening ENML (raw markup into the document)",
        '    return _collapse("".join(out))',
        "    return raw",
        UNIT,
    ),
]


def run(args):
    cmd = ["uv", "run", "pytest"] if shutil.which("uv") else [sys.executable, "-m", "pytest"]
    proc = subprocess.run(
        [
            *cmd,
            *args,
            "-q",
            "--no-header",
            "-x",
            "-p",
            "no:cacheprovider",
            "-W",
            "ignore::DeprecationWarning",
        ],
        cwd=PKG,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    return proc.returncode, proc.stdout + proc.stderr


def first_failure(output):
    for line in output.splitlines():
        if line.startswith("FAILED ") or " failed" in line:
            return line.strip()[:110]
    return "(no failure line found)"


def main():
    original = MODULE.read_text(encoding="utf-8")
    shutil.copy(MODULE, BACKUP)

    print(f"{'MUTATION':<62} RESULT")
    print("-" * 100)
    survived = []
    try:
        for label, old, new, tests in MUTATIONS:
            if old not in original:
                print(f"{label:<62} !! anchor not found")
                survived.append((label, "ANCHOR MISSING"))
                continue

            MODULE.write_text(original.replace(old, new, 1), encoding="utf-8")
            code, output = run(tests)
            if code == 0:
                verdict = "SURVIVED  <-- blind spot"
                survived.append((label, "SURVIVED"))
            else:
                verdict = f"caught    ({first_failure(output)})"
            print(f"{label:<62} {verdict}")
    finally:
        shutil.copy(BACKUP, MODULE)
        BACKUP.unlink(missing_ok=True)

    print("-" * 100)
    code, output = run(["tests/"])
    baseline = "clean" if code == 0 else "BROKEN"
    print(f"restored original -> baseline suite: {baseline}")

    if survived:
        print("\nBLIND SPOTS FOUND:")
        for label, why in survived:
            print(f"  - {label} ({why})")
        return 1
    print("\nAll 10 mutations were caught: the suite validates these behaviours.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
