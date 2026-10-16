# Connector tests

`test_gitlab.py` covers token validation, project discovery, resource selection,
and issue/MR/wiki fetching with mocked HTTP responses. These tests do not
require a token or network access. Run `uv run pytest tests/` from the package
directory.

Further coverage will accompany the remaining implementation:

- Authentication and access failures versus successfully empty results.
- Resource selection and document rendering for issues, MRs, comments, and wikis.
- Pagination, rate limits, retries, and configurable instance URLs.
- Incremental cursors, timestamp ties, and comment-only edits/deletions.
- Wiki edits and renames.
- Verified deletions; preserve prior memory on incomplete or unauthorized reads.
- Integration with document-mode ingestion and downstream deletion cleanup.
