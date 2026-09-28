# Bump the database adapters to a new cognee release: code changes

The workflow has already done the mechanical part of the bump: the cognee pins,
the adapter package versions, the version strings, and every `uv.lock` and
`poetry.lock`. Your job is the code: read what changed in cognee's adapter
interfaces between the two releases, bring the adapters' support in line with
it, and write a short report for the reviewer.

A person reviews the pull request. Keep the changes small and easy to review.
When you are unsure whether a change is right, leave the code alone and say so
in the report instead.

## Inputs

The workflow gives you:

- The old and new cognee versions.
- `.bump/interface_diff.patch`: the diff of cognee's adapter-facing code between
  the two release tags.
- `.bump/cognee/`: a clone of the cognee repository. Use
  `git -C .bump/cognee diff v<old> v<new> -- <path>`,
  `git -C .bump/cognee show v<new>:<path>` and
  `git -C .bump/cognee log v<old>..v<new> -- <path>` for more context.

The adapters live in `packages/graph/*`, `packages/vector/*` and
`packages/hybrid/*`. They implement cognee's `GraphDBInterface`
(`cognee/infrastructure/databases/graph/graph_db_interface.py`) and
`VectorDBInterface` (`cognee/infrastructure/databases/vector/vector_db_interface.py`).
The shared offline contract checks are in `packages/shared/contract_suite/`.

## What to do

1. Read `.bump/interface_diff.patch`. List what changed for adapters:
   - new abstract methods, and removed or renamed methods
   - changed method signatures or return shapes
   - new optional methods and capability flags that have a default in the base class
   - changed engine-factory arguments (`get_graph_engine.py`, `create_vector_engine.py`)
   - changed call sites in cognee core that call adapter methods
   - new or changed upstream contract tests under `cognee/tests/`
   Ignore pure refactors (typing modernization, import order, renamed exceptions
   that subclass the old ones).

2. Implement new **optional** methods only where the backend supports the
   operation natively and the base-class default is clearly worse (for example,
   it reads the whole graph into memory). Follow the docstring contract in the
   interface exactly. Don't implement a method just to override the default with
   the same behavior.

3. Update `packages/shared/contract_suite/` when the call shapes cognee uses
   changed, or when a new optional method needs a shape check. Follow the style
   of the existing checks. Keep the version references in its docstrings
   accurate.

4. If an upstream contract test that an adapter mirrors changed (for example
   `packages/graph/typedb/tests/integration/test_provenance_contract.py` mirrors
   `cognee/tests/integration/infrastructure/graph/test_graph_provenance_adapter_contract.py`),
   port the change.

5. Do **not** try to fix breaking changes (a removed method an adapter still
   calls, a base class that moved, a new abstract method). List them in the
   report with the affected files; the reviewer fixes them.

## Rules

- Only change files under `packages/graph/`, `packages/vector/`,
  `packages/hybrid/` and `packages/shared/contract_suite/`. Changes anywhere
  else are discarded.
- Don't touch `pyproject.toml`, `uv.lock` or `poetry.lock` files.
- Match the surrounding code: naming, comment density, docstring style.
- Code must pass `ruff check` and `ruff format` with the repository's `ruff.toml`.

## Report

Write `.bump/agent_summary.md` with these sections, in this order. Keep each
bullet to one or two lines. Write "None." for an empty section.

```markdown
### Interface changes (cognee v<old> → v<new>)
- ...

### Breaking changes for the reviewer
- `<adapter or file>`: what breaks and why.

### Implemented
- `<adapter>`: `<method>` — what it does natively.

### Not implemented
- `<method>`: why it was left to the base-class default.
```
