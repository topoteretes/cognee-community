"""Bump the community database adapters to a cognee release.

Used by .github/workflows/bump_cognee.yml; runnable locally from the repo root:

    python .github/scripts/bump_cognee.py resolve [--version X]
    python .github/scripts/bump_cognee.py apply --version X [--skip-locks]

`resolve` picks the target version (the latest stable cognee on PyPI unless
--version is given) and reports whether any adapter is behind it. `apply` does
the mechanical bump: cognee pins, pins of cognee's transitive dependencies,
adapter package versions, version strings in tests and docs, and every
uv.lock / poetry.lock. It writes a Markdown summary to .bump/summary.md.

Requires Python 3.11+ and `packaging`; `apply` also needs `uv`.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_GLOBS = ("packages/graph/*", "packages/vector/*", "packages/hybrid/*")
CONTRACT_SUITE = REPO_ROOT / "packages/shared/contract_suite"
WORKFLOWS_README = REPO_ROOT / ".github/workflows/README.md"
OUT_DIR = REPO_ROOT / ".bump"

COGNEE_PIN = re.compile(r'"cognee(?P<extras>\[[^\]]*\])?==(?P<version>[^"]+)"')
PROJECT_VERSION = re.compile(r'^version\s*=\s*"(?P<version>[^"]+)"', re.MULTILINE)
EXACT_PIN = re.compile(r'^(?P<indent>\s*)"(?P<name>[A-Za-z0-9_.\-]+)==(?P<version>[^"]+)"')

# The version cognee's own lock was written by in PR #210; keep lock churn down.
POETRY = os.environ.get("BUMP_POETRY", "uvx poetry==2.4.1").split()
LOCK_ATTEMPTS = 5


# --------------------------------------------------------------------------- PyPI


def _get_json(url: str, attempts: int = LOCK_ATTEMPTS):
    """GET a JSON document, retrying PyPI's transient 5xx responses. None on 404."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code < 500 or attempt == attempts:
                raise
        except urllib.error.URLError:
            if attempt == attempts:
                raise
        time.sleep(10 * attempt)
    return None


def _get_text(url: str) -> str:
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read().decode()
        except (urllib.error.HTTPError, urllib.error.URLError):
            if attempt == LOCK_ATTEMPTS:
                raise
            time.sleep(10 * attempt)
    raise RuntimeError("unreachable")


def latest_stable_cognee() -> str:
    """PyPI's info.version is the newest non-pre-release; double-check anyway."""
    data = _get_json("https://pypi.org/pypi/cognee/json")
    releases = [
        Version(version)
        for version, files in data["releases"].items()
        if files and not all(f.get("yanked") for f in files) and _is_stable(version)
    ]
    return str(max(releases))


def _is_stable(version: str) -> bool:
    try:
        parsed = Version(version)
    except InvalidVersion:
        return False
    return not (parsed.is_prerelease or parsed.is_devrelease)


def published_versions(package: str) -> tuple[set[Version], set[Version]]:
    """(live, taken): live excludes yanked releases, taken is every number PyPI
    will refuse to accept again."""
    data = _get_json(f"https://pypi.org/pypi/{package}/json")
    if not data:
        return set(), set()
    taken = {Version(v) for v, files in data["releases"].items() if files}
    live = {
        Version(v)
        for v, files in data["releases"].items()
        if files and not all(f.get("yanked") for f in files)
    }
    return live, taken


def wait_for_pypi(version: str, timeout: int = 900) -> None:
    """Locking needs the release's files on PyPI; right after an upload they can lag."""
    deadline = time.monotonic() + timeout
    while True:
        data = _get_json(f"https://pypi.org/pypi/cognee/{version}/json")
        if data and data.get("urls"):
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"cognee {version} has no files on PyPI after {timeout}s")
        time.sleep(30)


def cognee_release_metadata(version: str) -> tuple[set[str], dict[str, str]]:
    """Direct dependency names and locked versions of cognee at tag v<version>."""
    base = f"https://raw.githubusercontent.com/topoteretes/cognee/v{version}"
    pyproject = tomllib.loads(_get_text(f"{base}/pyproject.toml"))
    direct = {
        canonicalize_name(re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0])
        for requirement in pyproject["project"]["dependencies"]
    }
    locked: dict[str, str] = {}
    for package in tomllib.loads(_get_text(f"{base}/uv.lock")).get("package", []):
        locked.setdefault(canonicalize_name(package["name"]), package["version"])
    return direct, locked


# --------------------------------------------------------------------- discovery


def adapter_dirs() -> list[Path]:
    dirs = []
    for pattern in ADAPTER_GLOBS:
        dirs.extend(p for p in REPO_ROOT.glob(pattern) if (p / "pyproject.toml").is_file())
    return sorted(dirs)


def current_pins() -> dict[Path, str]:
    pins = {}
    for directory in adapter_dirs():
        match = COGNEE_PIN.search((directory / "pyproject.toml").read_text())
        if match:
            pins[directory] = match["version"]
    return pins


# ------------------------------------------------------------------- text edits


def _replace_cognee_mentions(text: str, old: str, new: str, series: bool = False) -> str:
    """`cognee==1.6.1`, `cognee == 1.6.1`, `cognee 1.6.1`, `cognee v1.6.1` and
    `cognee-1.6.1`. With series=True also `cognee 1.6.x`: only for the pin
    comments in pyproject.toml, since READMEs use that form for history."""
    old_escaped = re.escape(old)
    text = re.sub(
        rf"(cognee(?:\[[^\]]*\])?(?:\s*==\s*|\s+v?|-))(?:{old_escaped})(?![\d]|\.\d)",
        rf"\g<1>{new}",
        text,
    )
    old_series, new_series = ".".join(old.split(".")[:2]), ".".join(new.split(".")[:2])
    if series and old_series != new_series:
        text = re.sub(rf"(cognee\s+){re.escape(old_series)}\.x\b", rf"\g<1>{new_series}.x", text)
    return text


def _replace_exact(text: str, old: str, new: str) -> str:
    """Every standalone occurrence of the version, for files that only ever name
    the pinned version (contract suite, test_contract.py docstrings)."""
    return re.sub(rf"(?<![\d.]){re.escape(old)}(?![\d]|\.\d)", new, text)


def _next_package_version(name: str, current: str) -> str:
    """The next patch version after the newest live release. A version that is not
    on PyPI yet (and newer than every live release) is a pending bump; keep it.
    Yanked releases don't count as newest, but their numbers are never reused."""
    live, taken = published_versions(name)
    current_v = Version(current)
    if current_v not in taken and (not live or current_v > max(live)):
        return current
    candidate = max([current_v, *live])
    while True:
        candidate = Version(f"{candidate.major}.{candidate.minor}.{candidate.micro + 1}")
        if candidate not in taken:
            return str(candidate)


def bump_pyproject(
    directory: Path, old: str, new: str, locked: dict[str, str]
) -> tuple[str, str, str]:
    """Returns (package name, old package version, new package version)."""
    path = directory / "pyproject.toml"
    text = path.read_text()
    text = COGNEE_PIN.sub(lambda m: f'"cognee{m["extras"] or ""}=={new}"', text)

    # Pins of cognee's transitive dependencies ("Transitive via cognee; pinned to
    # the version cognee X locks"): move them to what the new release locks.
    lines = text.splitlines(keepends=True)
    comment_block: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            comment_block.append(stripped)
            continue
        pin = EXACT_PIN.match(line)
        transitive = any("transitive via cognee" in c.lower() for c in comment_block)
        if pin and transitive and pin["name"].lower() != "cognee":
            target = locked.get(canonicalize_name(pin["name"]))
            if target and target != pin["version"]:
                lines[index] = line.replace(f'=={pin["version"]}"', f'=={target}"', 1)
        comment_block = []
    text = _replace_cognee_mentions("".join(lines), old, new, series=True)

    name = tomllib.loads(text)["project"]["name"]
    old_package_version = PROJECT_VERSION.search(text)["version"]
    new_package_version = _next_package_version(name, old_package_version)
    if new_package_version != old_package_version:
        text = PROJECT_VERSION.sub(f'version = "{new_package_version}"', text, count=1)
        for init in directory.glob("*/__init__.py"):
            init_text = init.read_text()
            updated = init_text.replace(
                f'__version__ = "{old_package_version}"', f'__version__ = "{new_package_version}"'
            )
            if updated != init_text:
                init.write_text(updated)
    path.write_text(text)
    return name, old_package_version, new_package_version


def bump_text_files(directory: Path, old: str, new: str) -> None:
    for contract in directory.glob("tests/unit/test_contract.py"):
        contract.write_text(_replace_exact(contract.read_text(), old, new))
    readme = directory / "README.md"
    if readme.is_file():
        readme.write_text(_replace_cognee_mentions(readme.read_text(), old, new))


def bump_shared_files(old_versions: set[str], new: str) -> None:
    for old in old_versions:
        for path in CONTRACT_SUITE.glob("*"):
            if path.suffix in (".py", ".md"):
                path.write_text(_replace_exact(path.read_text(), old, new))
        WORKFLOWS_README.write_text(
            _replace_cognee_mentions(WORKFLOWS_README.read_text(), old, new)
        )


# ------------------------------------------------------------------------ locks


def _run_with_retries(command: list[str], cwd: Path) -> tuple[bool, str]:
    output = ""
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        output = (result.stdout + result.stderr)[-2000:]
        if result.returncode == 0:
            return True, output
        time.sleep(20 * attempt)  # PyPI 5xx surface as "package not found" in poetry
    return False, output


def _locked_names(lock_path: Path) -> set[str]:
    data = tomllib.loads(lock_path.read_text())
    return {canonicalize_name(p["name"]) for p in data.get("package", [])}


def regenerate_locks(directory: Path, upgrade: set[str]) -> list[str]:
    """Re-lock, and upgrade cognee's direct dependencies: a plain re-lock keeps any
    old version that still satisfies cognee's range (PR #210 kept a pydantic that
    cognee 1.6.1 fails to import with). Returns error messages."""
    errors = []
    uv_lock = directory / "uv.lock"
    if uv_lock.is_file():
        packages = sorted(({"cognee"} | upgrade) & _locked_names(uv_lock) | {"cognee"})
        command = ["uv", "lock", "--quiet"]
        for package in packages:
            command += ["--upgrade-package", package]
        ok, output = _run_with_retries(command, directory)
        if not ok:
            errors.append(f"`uv lock` failed:\n```\n{output}\n```")
    poetry_lock = directory / "poetry.lock"
    if poetry_lock.is_file():
        ok, output = _run_with_retries([*POETRY, "lock", "--no-interaction"], directory)
        if ok:
            packages = sorted(upgrade & _locked_names(poetry_lock))
            if packages:
                ok, output = _run_with_retries(
                    [*POETRY, "update", "--lock", "--no-interaction", *packages], directory
                )
        if not ok:
            errors.append(f"`poetry lock` failed:\n```\n{output}\n```")
    return errors


# ------------------------------------------------------------------- commands


def _set_output(**values: str) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    print(lines, end="")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(lines)


def cmd_resolve(args: argparse.Namespace) -> None:
    target = args.version or latest_stable_cognee()
    if not _is_stable(target):
        raise SystemExit(f"{target} is a pre-release; only stable releases are bumped")
    pins = current_pins()
    behind = {v for v in pins.values() if Version(v) < Version(target)}
    oldest = str(min(Version(v) for v in pins.values())) if pins else ""
    _set_output(
        version=target,
        old_version=oldest,
        needed="true" if behind else "false",
    )


def cmd_apply(args: argparse.Namespace) -> None:
    new = args.version
    if not args.skip_locks:
        wait_for_pypi(new)
    direct, locked = cognee_release_metadata(new)
    pins = {d: v for d, v in current_pins().items() if Version(v) < Version(new)}
    if not pins:
        raise SystemExit(f"every adapter already pins cognee>={new}")

    rows = []
    for directory, old in pins.items():
        name, old_pkg, new_pkg = bump_pyproject(directory, old, new, locked)
        bump_text_files(directory, old, new)
        rows.append((directory, name, old, old_pkg, new_pkg))
    bump_shared_files(set(pins.values()), new)

    errors: dict[Path, list[str]] = {}
    if not args.skip_locks:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = pool.map(lambda d: (d, regenerate_locks(d, direct)), pins)
            errors = {d: e for d, e in results if e}

    OUT_DIR.mkdir(exist_ok=True)
    summary = [
        "| Package | cognee | Package version | Locks |",
        "|---|---|---|---|",
    ]
    for directory, _name, old, old_pkg, new_pkg in rows:
        relative = directory.relative_to(REPO_ROOT)
        locks = [f for f in ("uv.lock", "poetry.lock") if (directory / f).is_file()]
        state = "skipped" if args.skip_locks else ("**failed**" if directory in errors else "ok")
        summary.append(
            f"| `{relative}` | {old} → {new} | {old_pkg} → {new_pkg} | "
            f"{', '.join(locks) or '—'}: {state if locks else '—'} |"
        )
    for directory, messages in errors.items():
        summary.append(f"\n**{directory.relative_to(REPO_ROOT)}**\n")
        summary.extend(messages)
    (OUT_DIR / "summary.md").write_text("\n".join(summary) + "\n")
    print("\n".join(summary))
    if errors:
        print(f"::warning::lock regeneration failed for {len(errors)} package(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve", help="pick the target version")
    resolve.add_argument("--version", default="", help="override PyPI's latest stable")
    resolve.set_defaults(func=cmd_resolve)
    apply = sub.add_parser("apply", help="bump the adapters")
    apply.add_argument("--version", required=True)
    apply.add_argument("--skip-locks", action="store_true", help="edit files only")
    apply.set_defaults(func=cmd_apply)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
