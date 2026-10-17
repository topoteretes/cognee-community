# Connector tests

`test_gitlab.py` covers token validation, project discovery, resource selection,
and issue/MR/wiki fetching with mocked HTTP responses. `test_documents.py`
checks resource mapping. `test_sync.py` exercises checkpoints, overlap, timestamp
ties, wiki hashes, verified deletion candidates, pagination-race retention, and
failure atomicity, and whole-project cleanup (including hidden projects returning
404). `test_source.py` verifies account scoping, DLT state persistence
across pipeline instances, merge updates, and hard deletions in temporary SQLite
staging. These tests require no credentials or network access.

Run `uv run pytest tests/` from this package directory.

Remaining coverage depends on future work: comment-only updates/deletions,
retry handling, and live GitLab-to-Cognee graph integration. The local staging
tests do not exercise LLM extraction or graph/vector deletion cleanup.
