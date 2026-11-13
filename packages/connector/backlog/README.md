# Cognee Community Connector: Backlog (Nulab)

Ingests [Nulab Backlog](https://backlog.com/) (`backlog.com` and `backlog.jp`) project issues, ordered comment threads, and Document pages (`/api/v2/documents`) into Cognee graph memory (`DOCUMENT_SOURCE_ATTR`, `write_disposition="merge"`).

## Setup

1. Generate a Personal API Key in Backlog under **Personal Settings > API**.
2. Set `BACKLOG_SPACE_URL` (e.g., `https://your-space.backlog.com` or `https://your-space.backlog.jp`) and `BACKLOG_API_KEY`.

## Key Capabilities

- **Incremental Sync (`updatedSince`)**: Tracks a per-project `updatedSince` cursor (`sort=updated&order=asc`) and re-fetches comments only for issues updated since the last successful sync.
- **Forget-on-Delete (`_deleted` Tombstones)**: Sweeps project issue IDs (`count=100` + `offset`) and emits `_deleted=True` tombstones for deleted issues. Network errors abort the sweep before deleting any live graph content.
- **Rate-Limit Backoff**: Parses `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` to throttle near the per-minute limit and wait until `Reset` on HTTP 429.
- **Security & Privacy**: Redacts `apiKey` query parameters from all logs, errors, and stored documents, and strips user email addresses (`mailAddress`), keeping display names only.
