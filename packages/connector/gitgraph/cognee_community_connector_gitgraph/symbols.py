"""Turn the Python files in a git tree into symbol rows and import/call reference rows."""

import ast
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

from git import Blob, GitCommandError
from gitdb.exc import BadName

from .git_history import _open

Row = dict[str, Any]
# (symbols, references, skip reason). References are candidates: the target may not exist.
Parsed = tuple[list[Row], list[Row], str | None]

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _module_name(path: str) -> str:
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _join(*parts: str) -> str:
    return ".".join(p for p in parts if p)


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _ref(path: str, owner: str | None, target: str, kind: str, line: int) -> Row:
    return {
        "id": f"{path}:{owner or ''}->{target}:{kind}",
        "from_file": path,
        "from_symbol": owner,
        "to_symbol": target,
        "kind": kind,
        "line": line,
    }


def _walk(path: str, tree: ast.Module, modules: dict[str, str]) -> tuple[list[Row], list[Row]]:
    package = _module_name(path).split(".") if _module_name(path) else []
    if not path.endswith("__init__.py"):
        package = package[:-1]
    symbols: list[Row] = []
    refs: list[Row] = []
    names: dict[str, str] = {}  # local name -> target symbol id
    mod_aliases: dict[str, str] = {}  # dotted local name -> module file path
    local: set[str] = set()
    calls: list[tuple[str | None, ast.expr, int]] = []
    methods: dict[str | None, str] = {}  # method symbol id -> class symbol id
    assigned: dict[tuple[str | None, str], list[ast.expr]] = {}  # (owner, name) -> `name = value`s
    stores: Counter[tuple[str | None, str]] = Counter()  # (owner, name) -> times bound

    def symbol(kind: str, qualname: str, node: ast.AST) -> str:
        sid = f"{path}::{qualname}"
        symbols.append(
            {
                "id": sid,
                "kind": kind,
                "name": qualname,
                "file": path,
                "line": node.lineno,  # type: ignore[attr-defined]
                "end_line": node.end_lineno,  # type: ignore[attr-defined]
            }
        )
        return sid

    def import_from(node: ast.ImportFrom, owner: str | None) -> None:
        up = (node.level or 0) - 1
        if up > len(package):
            return  # climbs above the repo root
        base = package[: len(package) - up] if up > 0 else package
        target = _join(*base, node.module or "") if node.level else node.module or ""
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            sub = _join(target, alias.name)
            if sub in modules:
                mod_aliases[bound] = modules[sub]
            elif target in modules:
                sid = f"{modules[target]}::{alias.name}"
                names[bound] = sid
                refs.append(_ref(path, owner, sid, "import", node.lineno))

    # Iterative walk (not recursion) so deeply nested source cannot blow the stack.
    # scope: "module" for top-level statements, "class:Name" for a top-level class body.
    stack: list[tuple[ast.AST, str | None, str]] = [(n, None, "module") for n in tree.body[::-1]]
    while stack:
        node, owner, scope = stack.pop()
        child_scope = "nested"
        if isinstance(node, _FUNCS):
            # ponytail: only top-level functions/classes and their direct methods become
            # symbols; nested functions and classes are skipped.
            if scope == "module":
                owner = symbol("function", node.name, node)
                local.add(node.name)
            elif scope.startswith("class:"):
                owner = symbol("method", f"{scope[6:]}.{node.name}", node)
                methods[owner] = f"{path}::{scope[6:]}"
        elif isinstance(node, ast.ClassDef):
            if scope == "module":
                symbol("class", node.name, node)
                local.add(node.name)
                child_scope = f"class:{node.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    mod_aliases[alias.asname or alias.name] = modules[alias.name]
            continue
        elif isinstance(node, ast.ImportFrom):
            import_from(node, owner)
            continue
        elif isinstance(node, ast.Call):
            calls.append((owner, node.func, node.lineno))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned.setdefault((owner, t.id), []).append(node.value)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stores[(owner, node.id)] += 1
        body = getattr(node, "body", None) if isinstance(node, ast.ClassDef) else None
        for child in ast.iter_child_nodes(node):
            in_body = isinstance(body, list) and child in body
            stack.append((child, owner, child_scope if in_body else "nested"))

    def resolve(func: ast.expr) -> str | None:  # `name` or `mod_alias.name` -> symbol id
        if isinstance(func, ast.Name):
            if func.id in names:
                return names[func.id]
            if func.id in local:
                return f"{path}::{func.id}"
        elif isinstance(func, ast.Attribute):
            dotted = _dotted(func.value)
            if dotted is not None and dotted in mod_aliases:
                return f"{mod_aliases[dotted]}::{func.attr}"
        return None

    def instance_of(owner: str | None, name: str) -> str | None:
        # Only if every binding of the name in this scope is a constructor call of one class.
        values = assigned.get((owner, name), [])
        classes = {resolve(v.func) if isinstance(v, ast.Call) else None for v in values}
        return classes.pop() if len(classes) == 1 and len(values) == stores[(owner, name)] else None

    # ponytail: no type inference across functions or attributes like self.client.m().
    for owner, func, line in calls:
        target = resolve(func)
        if target is None and isinstance(func, ast.Attribute):
            base = func.value
            if isinstance(base, ast.Name) and base.id in ("self", "cls") and owner in methods:
                cls: str | None = methods[owner]
            elif isinstance(base, ast.Name) and (owner, base.id) in stores:
                cls = instance_of(owner, base.id)
            else:
                cls = resolve(base)  # Cls.m() or mod_alias.Cls.m()
            if cls is not None:
                target = f"{cls}.{func.attr}"
        if target is not None:
            refs.append(_ref(path, owner, target, "call", line))
    return symbols, refs


def parse_module(path: str, source: bytes, modules: dict[str, str]) -> Parsed:
    """Parse one Python file; ``modules`` maps module name -> file path for the whole tree.

    Never raises for bad source: a file that cannot be read gives a skip reason instead.
    """
    if b"\0" in source:
        return [], [], "contains null bytes"
    try:
        text = source.decode("utf-8-sig")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text, filename=path)
        symbols, refs = _walk(path, tree, modules)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return [], [], f"{type(exc).__name__}: {exc}"[:200]
    return symbols, refs, None


def extract_symbols(repo_path: str | Path, ref: str) -> dict[str, list[Row]]:
    """Return ``{"symbols", "references", "skipped"}`` for the ``*.py`` files at ``ref``.

    Only top-level functions and classes and their direct methods are symbols. References are
    import and call edges whose target symbol exists at ``ref``.
    """
    repo = _open(repo_path)
    try:
        tree = repo.commit(ref).tree
    except (BadName, ValueError, GitCommandError) as exc:
        raise ValueError(f"unknown ref: {ref}") from exc
    blobs = {
        str(b.path): b
        for b in tree.traverse()
        if isinstance(b, Blob) and str(b.path).endswith(".py")
    }
    modules: dict[str, str] = {}
    for path in sorted(blobs):
        if _module_name(path):
            modules.setdefault(_module_name(path), path)
    symbols: list[Row] = []
    candidates: list[Row] = []
    skipped: list[Row] = []
    for path in sorted(blobs):
        found, refs, reason = parse_module(path, blobs[path].data_stream.read(), modules)
        if reason is not None:
            skipped.append({"file": path, "reason": reason})
        symbols += found
        candidates += refs
    known = {s["id"] for s in symbols}
    deduped: dict[str, Row] = {}
    for row in candidates:
        if row["to_symbol"] not in known:
            continue
        kept = deduped.get(row["id"])
        if kept is None or row["line"] < kept["line"]:
            deduped[row["id"]] = row
    return {
        "symbols": sorted(symbols, key=lambda s: (s["file"], s["line"], s["id"])),
        "references": [deduped[k] for k in sorted(deduped)],
        "skipped": sorted(skipped, key=lambda s: s["file"]),
    }
