"""Obsidian vault connector for cognee — a ``dlt`` source that turns a vault into memory.

Sync a local Obsidian vault (markdown notes, YAML frontmatter, ``[[wikilinks]]``)
into cognee, incrementally and with forget-on-deletion — "ask my vault".  Built
entirely on the existing DLT ingestion subsystem, like the sibling Google Drive
and Confluence connectors; the resource produced here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_obsidian import obsidian_source

    await cognee.remember(
        obsidian_source(vault_path="~/Documents/MyVault"),
        dataset_name="my_vault",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by note path
        max_rows_per_table=0,        # 0 = no row cap (vaults often exceed the default 50)
    )

Design
------
* **Auth** — none.  The vault is a local directory; the connector only reads it.
* **Primary key** — the note's vault-relative path.  Obsidian has no stable
  note id — path *is* identity — so a rename is a delete + create by
  construction (documented in the README rather than papered over).
* **Ingest** — every ``*.md`` under the vault.  YAML frontmatter becomes record
  metadata (kept as a ``metadata`` JSON column and folded into the note text as
  a ``Properties`` section so it survives into entity extraction); the body is
  the content.
* **Wikilinks → edges** — ``[[note]]``, ``[[note|alias]]`` (target is ``note``,
  not the alias), ``[[note#heading]]`` (target is still ``note``), and embeds
  ``![[note]]`` (an edge plus a transclusion flag).  Resolution is *name*-based,
  like Obsidian itself: ``[[foo]]`` resolves to a ``foo.md`` anywhere in the
  vault (case-insensitive, shortest path on ambiguity), so the link index is
  built vault-wide before any row is emitted.  Resolved links are emitted as a
  structured ``links`` JSON column *and* rendered into the note text as
  explicit "Links to / Embeds" statements, which is what turns them into
  note→note edges in the extracted graph instead of being discarded.
* **Incremental cursor** — a per-file ``(mtime, size, sha256)`` map kept in
  dlt's per-resource state.  ``mtime`` + ``size`` are the cheap fast path; the
  hash is authoritative.  Sync tools (Syncthing, iCloud) rewrite mtimes and can
  move them *backwards*, so any mtime change — forward or backward — falls
  through to the hash, and only a real content change re-emits the row.
* **Forget-on-delete** — the previous run's path snapshot lives in the same
  state; paths absent from the current walk are emitted as ``_deleted``
  hard-delete tombstones, dlt drops them on ``merge``, and cognee's existing
  ``orphan_cleanup`` purges them from the graph + vector + relational stores.
* **Exclusions** — ``.obsidian/``, ``.trash/``, and ``*.sync-conflict-*`` files
  by default.  The last one matters in practice: any vault synced with
  Syncthing/iCloud grows conflict copies that are near-duplicates of live
  notes, and ingesting them poisons dedup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("obsidian_connector")

# dlt resource / staging-table name for Obsidian notes.
OBSIDIAN_TABLE_NAME = "obsidian_notes"
OBSIDIAN_SOURCE_NAME = "obsidian"

# Directories never walked, at any depth: Obsidian's own config and its trash.
DEFAULT_EXCLUDED_DIRS = frozenset({".obsidian", ".trash"})

# File-sync conflict copies (Syncthing et al.) — near-duplicates of live notes.
_SYNC_CONFLICT_MARKER = ".sync-conflict-"

# The three wikilink forms: [[target]], [[target#heading]], [[target|alias]]
# (heading and alias may combine); a leading ``!`` marks an embed.
_WIKILINK_RE = re.compile(
    r"(?P<embed>!?)\[\[(?P<target>[^\[\]|#]+)(?:#(?P<heading>[^\[\]|]*))?(?:\|(?P<alias>[^\[\]]*))?\]\]"
)

# Frontmatter block: a leading ``---`` line, YAML, and a closing ``---``/``...``.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n(?:---|\.\.\.)\s*(?:\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class _VaultConfig:
    vault_path: Path
    excluded_dirs: frozenset[str] = DEFAULT_EXCLUDED_DIRS
    extra_excludes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WikiLink:
    """One parsed wikilink occurrence in a note body."""

    target: str  # raw link target as written (no heading/alias)
    heading: str | None = None
    alias: str | None = None
    embed: bool = False  # True for ![[transclusions]]


# ---------------------------------------------------------------------------
# Vault walk / exclusions
# ---------------------------------------------------------------------------
def _is_excluded(relative_path: Path, config: _VaultConfig) -> bool:
    """True when the note must not be ingested (config dirs, trash, conflicts)."""
    if any(part in config.excluded_dirs for part in relative_path.parts):
        return True
    if _SYNC_CONFLICT_MARKER in relative_path.name:
        return True
    posix = relative_path.as_posix()
    return any(Path(posix).match(pattern) for pattern in config.extra_excludes)


def _walk_vault(config: _VaultConfig) -> list[str]:
    """Return the sorted vault-relative POSIX paths of all ingestable ``*.md`` files."""
    paths: list[str] = []
    for root, dirs, files in os.walk(config.vault_path):
        root_path = Path(root)
        # Prune excluded directories in place so os.walk never descends into them.
        dirs[:] = [
            d
            for d in dirs
            if not _is_excluded((root_path / d).relative_to(config.vault_path), config)
        ]
        for name in files:
            if not name.lower().endswith(".md"):
                continue
            relative = (root_path / name).relative_to(config.vault_path)
            if _is_excluded(relative, config):
                continue
            paths.append(relative.as_posix())
    return sorted(paths)


# ---------------------------------------------------------------------------
# Parsing: frontmatter + wikilinks
# ---------------------------------------------------------------------------
def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a note into (frontmatter dict, body).

    Malformed YAML is tolerated — the note is still ingested, with the raw
    block left in the body so nothing is silently dropped.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    try:
        import yaml

        loaded = yaml.safe_load(match.group(1))
    except Exception:  # yaml missing or malformed frontmatter
        logger.warning("Obsidian: unparseable frontmatter left in note body.")
        return {}, text

    if not isinstance(loaded, dict):
        # Frontmatter that is a bare scalar/list is not property metadata.
        return {}, text[match.end() :]
    return loaded, text[match.end() :]


def _extract_wikilinks(body: str) -> list[WikiLink]:
    """Parse all wikilink occurrences (plain, aliased, heading, embed) in order."""
    links: list[WikiLink] = []
    for match in _WIKILINK_RE.finditer(body):
        target = match.group("target").strip()
        if not target:
            continue
        links.append(
            WikiLink(
                target=target,
                heading=(match.group("heading") or "").strip() or None,
                alias=(match.group("alias") or "").strip() or None,
                embed=match.group("embed") == "!",
            )
        )
    return links


# ---------------------------------------------------------------------------
# Vault-wide link resolution (name-based, like Obsidian)
# ---------------------------------------------------------------------------
def _build_link_index(paths: list[str]) -> dict[str, str]:
    """Map lowercase note names AND vault paths to a canonical relative path.

    Obsidian resolves ``[[foo]]`` to the closest ``foo.md`` anywhere in the
    vault, case-insensitively; when several notes share a name it prefers the
    shortest path.  ``[[dir/foo]]`` (a path link) resolves by path.  Iterating
    sorted paths and preferring fewer parts, then the shorter/lexicographically
    smaller path, makes resolution deterministic.
    """
    by_name: dict[str, str] = {}
    for path in sorted(paths, key=lambda p: (len(Path(p).parts), len(p), p)):
        stem_key = Path(path).stem.lower()
        by_name.setdefault(stem_key, path)
        # Path-style keys, with and without the .md suffix.
        posix = path.lower()
        by_name.setdefault(posix, path)
        by_name.setdefault(posix[: -len(".md")], path)
    return by_name


def _resolve_link(link: WikiLink, index: dict[str, str]) -> str | None:
    """Resolve a wikilink target to a vault-relative path, or None if unresolved."""
    return index.get(link.target.strip("/").lower())


# ---------------------------------------------------------------------------
# Row rendering
# ---------------------------------------------------------------------------
def _render_note(
    relative_path: str,
    frontmatter: dict[str, Any],
    body: str,
    links: list[WikiLink],
    index: dict[str, str],
) -> dict[str, Any]:
    """Flatten one note into a document-mode dlt row.

    The row contract is ``{id, title, content, url}`` (see
    ``resolve_dlt_sources``), plus two structured columns: ``metadata``
    (frontmatter as JSON) and ``links`` (resolved wikilinks as JSON).  Resolved
    links are also rendered into the content as explicit "Links to / Embeds"
    statements so entity extraction turns them into note→note edges.
    """
    title = str(frontmatter.get("title") or Path(relative_path).stem)

    resolved_links: list[dict[str, Any]] = []
    linked_titles: list[str] = []
    embedded_titles: list[str] = []
    for link in links:
        resolved = _resolve_link(link, index)
        resolved_links.append(
            {
                "target": resolved,  # None when the link points at no existing note
                "raw": link.target,
                "heading": link.heading,
                "alias": link.alias,
                "embed": link.embed,
            }
        )
        display = Path(resolved).stem if resolved else link.target
        bucket = embedded_titles if link.embed else linked_titles
        if display not in bucket:
            bucket.append(display)

    sections = [body.strip()]
    if frontmatter:
        properties = "\n".join(f"- {key}: {_scalar(value)}" for key, value in frontmatter.items())
        sections.append(f"Properties:\n{properties}")
    if linked_titles:
        sections.append(f"Links to notes: {', '.join(linked_titles)}.")
    if embedded_titles:
        sections.append(f"Embeds (transcludes) notes: {', '.join(embedded_titles)}.")

    return {
        "id": relative_path,
        "title": title,
        "content": "\n\n".join(part for part in sections if part),
        # Obsidian's own URI scheme, vault-relative — stable when the vault
        # directory moves (an absolute file:// URI would churn every cursor).
        "url": f"obsidian://open?file={quote(relative_path[: -len('.md')])}",
        "metadata": json.dumps(frontmatter, sort_keys=True, default=str),
        "links": json.dumps(resolved_links, sort_keys=True),
        "_deleted": False,
    }


def _scalar(value: Any) -> str:
    """Render a frontmatter value as one readable line."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _row_fingerprint(row: dict[str, Any]) -> str:
    """Content hash of everything the row emits — the authoritative cursor half.

    Hashing the *rendered* row (not the raw file) means a note is also
    re-emitted when its content is byte-identical but its link resolution
    changed — e.g. a previously-unresolved ``[[foo]]`` starts resolving because
    ``foo.md`` was just created.
    """
    material = "\x00".join(
        (row["title"], row["content"], row["metadata"], row["links"], row["url"])
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Sync (pure given a config + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_notes(config: _VaultConfig, state: dict) -> Iterator[dict[str, Any]]:
    """Yield changed notes since the last run, plus hard-delete tombstones.

    ``state["files"]`` maps each known vault-relative path to its cursor entry
    ``{"mtime", "size", "sha256"}``.  A note whose mtime *and* size are
    unchanged is skipped without being read — unless the vault's set of note
    names changed since the last run (a create/delete/rename can silently
    change how other notes' wikilinks resolve, so the fast path is disabled
    for that run and every note is re-rendered; unchanged fingerprints are
    still not re-emitted).  Any mtime change — forward *or backward* — falls
    through to the fingerprint, which alone decides re-emission.
    """
    known: dict[str, dict[str, Any]] = dict(state.get("files", {}))
    current_paths = _walk_vault(config)

    if known and not current_paths:
        # Mirrors the Confluence guard: an empty walk over a previously
        # populated vault is far more likely a mount/permissions hiccup than a
        # genuine wipe; emitting len(known) tombstones would purge the dataset.
        logger.warning(
            "Obsidian: walk found 0 notes but %d were known; skipping deletion "
            "this run to avoid a mass forget-on-delete on a transient walk.",
            len(known),
        )
        return

    index = _build_link_index(current_paths)
    names_digest = hashlib.sha256("\n".join(current_paths).encode("utf-8")).hexdigest()
    fast_path_ok = state.get("names_digest") == names_digest

    changed = 0
    fresh: dict[str, dict[str, Any]] = {}
    for path in current_paths:
        absolute = config.vault_path / path
        try:
            stat = absolute.stat()
        except OSError:
            # Deleted between walk and stat — the next run tombstones it.
            continue
        entry = known.get(path)

        if (
            fast_path_ok
            and entry
            and entry.get("mtime") == stat.st_mtime
            and entry.get("size") == stat.st_size
        ):
            fresh[path] = entry
            continue

        text = absolute.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = _split_frontmatter(text)
        row = _render_note(path, frontmatter, body, _extract_wikilinks(body), index)
        fingerprint = _row_fingerprint(row)
        fresh[path] = {"mtime": stat.st_mtime, "size": stat.st_size, "sha256": fingerprint}

        if entry and entry.get("sha256") == fingerprint:
            # mtime moved (a sync tool touched the file, possibly backwards)
            # but the content is identical — refresh the cursor, emit nothing.
            continue
        changed += 1
        yield row

    deleted = sorted(set(known) - set(fresh))
    for path in deleted:
        yield {"id": path, "_deleted": True}

    state["files"] = fresh
    state["names_digest"] = names_digest
    logger.info("Obsidian: %d changed note(s), %d deletion(s).", changed, len(deleted))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def obsidian_source(
    vault_path: str | os.PathLike | None = None, *, exclude: list[str] | None = None
):
    """Return a ``dlt`` resource yielding one row per Obsidian note for ``remember``.

    Args:
        vault_path: Path to the vault directory. Falls back to the
            ``OBSIDIAN_VAULT_PATH`` environment variable.
        exclude: Extra glob patterns (matched against vault-relative POSIX
            paths) to skip, on top of the built-in ``.obsidian/``, ``.trash/``
            and ``*.sync-conflict-*`` exclusions.

    Returns:
        A ``dlt`` resource (``obsidian_notes``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and a ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Obsidian connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    resolved = vault_path or os.getenv("OBSIDIAN_VAULT_PATH")
    if not resolved:
        raise ValueError("vault_path is required (pass it explicitly or set OBSIDIAN_VAULT_PATH).")
    root = Path(resolved).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Obsidian vault not found or not a directory: {root}")

    config = _VaultConfig(vault_path=root, extra_excludes=tuple(exclude or ()))

    @dlt.resource(
        name=OBSIDIAN_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # `_deleted` is a boolean hard-delete marker (matching the Google
        # Drive / Confluence connectors): rows where it is True are removed
        # from the dlt destination on merge, which propagates the deletion
        # through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def obsidian_notes():
        yield from sync_notes(config, dlt.current.resource_state())

    resource = obsidian_notes()
    # Opt into the document ingestion path: each note row (id/title/content/url)
    # becomes a text document that flows through normal cognify (LLM graph
    # extraction). resolve_dlt_sources reads this marker; it never imports this
    # connector. Sync stays incremental — hand this to remember() with
    # write_disposition="merge" (per-file cursor + _deleted hard-delete).
    setattr(resource, DOCUMENT_SOURCE_ATTR, OBSIDIAN_SOURCE_NAME)
    return resource
