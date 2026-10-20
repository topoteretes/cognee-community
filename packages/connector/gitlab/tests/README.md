# Connector tests

`test_gitlab.py` covers token validation, project discovery, resource selection,
and issue/MR/wiki fetching with mocked HTTP responses. `test_documents.py`
checks resource mapping and comment rendering. `test_sync.py` exercises
checkpoints, overlap, timestamp ties, wiki hashes, verified deletion candidates,
pagination-race retention, failure atomicity, whole-project cleanup (including
hidden projects returning 404), and comment changes: notes are re-read when an
item's `updated_at` or `user_notes_count` changes, including a deleted comment
that only lowers the count. `test_source.py` verifies account scoping, DLT state
persistence across pipeline instances, merge updates, and hard deletions in
temporary SQLite staging. These tests require no credentials or network access.

Run `uv run --extra dev pytest tests/` from this package directory.

Not covered here: automatic retries (the connector has none; a 429 aborts the
run), LLM extraction, and graph/vector cleanup in Cognee. The live end-to-end
deletion check is described in the package README under "Live validation".
