"""DLT source for Obsidian vaults (full-snapshot sync + forget-on-delete).

Walks a local Obsidian vault for markdown notes, splits YAML frontmatter for
the title/tags, and yields each note as a dlt resource for cognee's ingestion
pipeline. No auth, no network — the vault is just files.

Like the Notion connector, notes are ingested as *normal documents*: the source
declares ``cognee_document_source = "obsidian"``, so ``resolve_dlt_sources``
tags each row ``system_metadata["source"] = "obsidian"`` (not ``"dlt"``).
``is_dlt_sourced`` therefore returns False and each note flows through the
standard cognify entity-extraction pipeline — the right treatment for prose —
instead of the deterministic dlt-row schema-context path.

Wikilinks are already a graph: ``[[target]]`` (plus the ``[[target|alias]]``,
``[[target#heading]]``, and ``![[embed]]`` forms) are extracted per note and
appended to the document text as a "Related notes" line, so cognify sees the
edges instead of discarding them.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the notes currently on disk each run. A deleted note simply drops
out of the snapshot and cognee's existing ``orphan_cleanup`` removes it from
the graph and vector stores. Unchanged notes keep a stable content-hash
``data_id``, so they are not re-ingested or re-cognified. (A vault has no
delete feed, so a merge + ``hard_delete`` approach cannot see deletions —
hence the Slack-style full-snapshot model.)
"""

import re
from pathlib import Path

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("obsidian_connector")

# dlt resource / staging-table name for vault notes.
OBSIDIAN_TABLE_NAME = "obsidian_notes"
OBSIDIAN_SOURCE_NAME = "obsidian"

# Directories never descended into (vault metadata, trash, VCS).
_DEFAULT_EXCLUDE_DIRS = frozenset({".obsidian", ".trash", ".git"})

# [[target]], [[target|alias]], [[target#heading]], ![[embed]]. Group 1 is the
# raw target; the section and alias parts are stripped when normalizing.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")

# Basename extensions that mark an embed as a file attachment (not a note).
# Note embeds (![[other note]]) have no extension and are kept — they are
# still graph edges.
_ATTACHMENT_EXTENSIONS = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "webp",
        "bmp",
        "ico",
        "pdf",
        "mp3",
        "wav",
        "ogg",
        "mp4",
        "mov",
        "webm",
        "zip",
        "csv",
        "xlsx",
        "pptx",
    }
)

# YAML frontmatter block at the very start of a note.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)

# First ATX heading, used as a title fallback when frontmatter has no title.
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)

_EXTRA_HINT = (
    'The Obsidian connector requires dlt: pip install "cognee-community-connector-obsidian" '
    "(provides dlt and pyyaml)."
)


def obsidian_source(
    vault_path: str | Path,
    include: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
):
    """Create a dlt source that yields Obsidian notes as markdown documents.

    Args:
        vault_path: Path to the Obsidian vault (a directory of ``.md`` files).
        include: Optional glob patterns (matched against the ``/``-separated
            path relative to the vault, e.g. ``["projects/*.md"]``) selecting
            which notes to ingest. When omitted, every ``.md`` file in the
            vault is ingested.
        exclude_dirs: Optional directory names to skip (matched by name at any
            depth). Defaults to ``[".obsidian", ".trash", ".git"]``.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    root = Path(vault_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Obsidian vault not found (not a directory): {vault_path}")
    skipped = frozenset(exclude_dirs) if exclude_dirs is not None else _DEFAULT_EXCLUDE_DIRS

    @dlt.resource(name=OBSIDIAN_TABLE_NAME, primary_key="id", write_disposition="replace")
    def obsidian_notes():
        # Full-snapshot sync: each run replaces staging with exactly the notes
        # currently on disk. Deleted/renamed notes fall out of staging and
        # cognee's orphan_cleanup then forgets them from the graph + vector
        # stores. Unchanged notes keep a stable content-hash data_id, so they
        # are not re-ingested/re-cognified.
        #
        # An unreadable note is NOT swallowed: because staging is authoritative
        # (replace), a note missing from a partial snapshot would be forgotten
        # as if deleted. Letting the error abort the run leaves staging — and
        # memory — untouched, which is the safe failure. Malformed content
        # (bad encoding, broken frontmatter) degrades gracefully to plain text
        # inside _note_to_row instead.
        count = 0
        for path in _iter_notes(root, include, skipped):
            count += 1
            yield _note_to_row(root, path)
        logger.info("Obsidian: synced %d note(s) from %s.", count, root)

    @dlt.source(name=OBSIDIAN_SOURCE_NAME)
    def _obsidian():
        return obsidian_notes

    source = _obsidian()
    # Opt into the document ingestion path (note → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, OBSIDIAN_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Vault scanning / note parsing (module-private)
# ---------------------------------------------------------------------------


def _iter_notes(root: Path, include: list[str] | None, skipped: frozenset[str]):
    """Yield ``.md`` note paths under *root*, deterministically ordered."""
    from fnmatch import fnmatchcase

    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in skipped or part.startswith(".") for part in rel.parts[:-1]):
            continue
        # Always skip dot-directories (e.g. a stray ".stversions"); the file
        # itself may still start with a dot, which is fine.
        rel_posix = rel.as_posix()
        if include and not any(fnmatchcase(rel_posix, pat) for pat in include):
            continue
        yield path


def _note_to_row(root: Path, path: Path) -> dict:
    """Flatten a vault note into a document row.

    Only ``id``/``title``/``tags`` (+ ``content``) are kept, so touching a note
    without changing its text does not churn the content-hash data_id.
    """
    rel_posix = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    metadata, body = _split_frontmatter(text)
    links = _extract_wikilinks(body)
    return {
        "id": rel_posix,
        "title": _note_title(metadata, body, path),
        "tags": _note_tags(metadata),
        "content": _note_content(body, links),
    }


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``(metadata, body)``; malformed frontmatter degrades to ``({}, text)``."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        import yaml
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc
    try:
        metadata = yaml.safe_load(match.group(1))
    except Exception:
        logger.warning("Obsidian: ignoring malformed frontmatter; ingesting as plain text.")
        return {}, text
    if not isinstance(metadata, dict):
        return {}, text
    return metadata, text[match.end() :]


def _note_title(metadata: dict, body: str, path: Path) -> str:
    """Note title: frontmatter ``title`` → first ``# heading`` → file stem."""
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    heading = _HEADING_RE.search(body)
    if heading:
        return heading.group(1).strip()
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.stem


def _note_tags(metadata: dict) -> str:
    """Comma-separated tag list from frontmatter (``tags``/``tag``), normalized."""
    raw = metadata.get("tags", metadata.get("tag", []))
    if isinstance(raw, str):
        # "a, b" or "#a #b" or "a b"
        raw = re.split(r"[,\s]+", raw)
    if not isinstance(raw, (list, tuple)):
        return ""
    tags = sorted({str(tag).lstrip("#").strip() for tag in raw if str(tag).strip()})
    return ", ".join(tags)


def _extract_wikilinks(body: str) -> list[str]:
    """Ordered, de-duplicated wikilink targets (aliases/sections stripped)."""
    links: list[str] = []
    for match in _WIKILINK_RE.finditer(body):
        target = match.group(1).split("#", 1)[0].split("|", 1)[0].strip()
        if not target:
            continue
        extension = target.rsplit("/", 1)[-1].rsplit(".", 1)
        if len(extension) == 2 and extension[1].lower() in _ATTACHMENT_EXTENSIONS:
            continue
        if target not in links:
            links.append(target)
    return links


def _note_content(body: str, links: list[str]) -> str:
    """Note body plus a Related-notes line so cognify sees the wikilink edges."""
    content = body.strip()
    if links:
        content += "\n\nRelated notes: " + ", ".join(links)
    return content
