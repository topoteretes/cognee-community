"""Regression test for per-call isolation in extract_code_parts.

extract_code_parts used a mutable default argument (`existing_nodes={}`), so the same dict was
reused across every call. Definitions with the same name in different files therefore deduplicated
against each other, and the nodes yielded for later files kept the ``file_path`` of whichever file
was parsed first. This test parses two separate files that both define ``shared`` and asserts that
the parts yielded for the second file carry the second file's path.

It runs entirely in-process with tree-sitter, so it needs no LLM provider or network access.
"""

import asyncio

import tree_sitter_python as tspython
from cognee_community_tasks_codify.get_local_dependencies import extract_code_parts
from tree_sitter import Language, Parser

SOURCE = "def shared():\n    pass\n"


def _parse_root(source: str):
    parser = Parser(Language(tspython.language()))
    return parser.parse(source.encode("utf-8")).root_node


async def _collect(root, script_path: str) -> list:
    return [part async for part in extract_code_parts(root, script_path=script_path)]


async def test_same_named_definitions_in_different_files_are_isolated():
    # Parse file A first so a shared cache would already contain "shared" when file B is parsed.
    await _collect(_parse_root(SOURCE), script_path="/proj/a.py")
    parts_b = await _collect(_parse_root(SOURCE), script_path="/proj/b.py")

    assert parts_b, "no code parts were extracted from the second file"
    foreign = [p for p in parts_b if getattr(p, "file_path", None) != "/proj/b.py"]
    assert not foreign, (
        "definitions from the second file carry a foreign file_path (shared-state leak): "
        f"{[getattr(p, 'file_path', None) for p in foreign]}"
    )


async def _main() -> None:
    await test_same_named_definitions_in_different_files_are_isolated()
    print("PASSED: test_same_named_definitions_in_different_files_are_isolated")


if __name__ == "__main__":
    asyncio.run(_main())
