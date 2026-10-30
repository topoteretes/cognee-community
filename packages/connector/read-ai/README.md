# Read.ai connector for Cognee

Import selected Read.ai meeting summaries, transcripts and metrics as searchable Cognee documents. Synchronization performs **full upstream reconciliation with incremental downstream processing**: every selected meeting is checked on each run, while unchanged document content is not re-ingested or re-embedded.

This package implements [cognee issue #4742](https://github.com/topoteretes/cognee/issues/4742) in **cognee-community**. The issue's API-key wording differs from Read.ai's current API: this connector uses **OAuth 2.1**, not static API keys. The provider is in open beta. See [API_RESEARCH.md](API_RESEARCH.md) for cited contracts and uncertainties.

## Requirements and setup

- Python 3.11–3.13, Cognee **1.6.4** with document-sync version 2, and dlt **1.30.x**.
- SQLite, Ladybug and LanceDB for the initially verified storage combination. Unsupported verification capabilities fail before storage mutation.
- Access to the selected meeting reports. Workspaces must enable **Downloads** under **Workspace Settings → Reports & Sharing**. [Provider prerequisites](https://support.read.ai/hc/en-us/articles/49379985941523-Read-AI-API-and-MCP-Overview)
- Cognee LLM and embedding configuration for a live run and graph-completion search. The offline example and tests supply synthetic responses and require no provider/model credentials.

From the checkout:

```bash
cd packages/connector/read-ai
uv sync --locked
uv run read-ai-connect
```

`read-ai-connect` registers the documented OAuth client, saves its client ID/secret before proceeding, and directs you to Read.ai's hosted authorization page. Enter the client ID and secret from the protected file there. Paste **only** the returned authorization code and PKCE verifier into the hidden prompts; the connector does not execute copied shell commands. If your authorization request used a nonce, provide its original value at the nonce prompt.

The default token file is `$XDG_CONFIG_HOME/cognee-read-ai/credentials.json`, or `~/.config/cognee-read-ai/credentials.json`. Override it with `--credentials /private/path/credentials.json`. Setup refuses a Git checkout or an existing credential file: choose a fresh private path for reauthorization. Files use mode `0600`, refresh is process-locked, and each rotated refresh token is atomically persisted before use. Timeout/server-error refresh requests are retried immediately once; permanent OAuth failures require reauthorization. [Authentication flow](https://support.read.ai/hc/en-us/articles/49380809380371-API-Keys-Authentication)

A new connection requires an RS256-validated Read.ai ID token, the expected client audience and issuer, and a matching authenticated UserInfo subject. Applicable nonce, authorized-party and access-token hash checks are enforced. Later runs bind authenticated UserInfo to the stored `(issuer, subject)`; another account cannot replay the old connection's pending data. If initial ID-token proof expires before the first synchronization and refresh does not issue fresh proof, reconnect rather than bypassing validation.

## Required synchronization entrypoint

```python
import asyncio
import cognee
from cognee_community_connector_read_ai import read_ai_source, sync_read_ai


async def main():
    source = read_ai_source(
        connection_id="my-read-account",
        start_time="2026-01-01T00:00:00Z",
        # end_time=None keeps future meetings in scope.
        include_summary=True,
        include_transcript=True,
        include_metrics=True,
    )
    result = await sync_read_ai(source, dataset_name="read-meetings")
    print(result)
    answers = await cognee.search(
        query_text="What decisions were made in these meetings?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["read-meetings"],
    )
    print(answers)


asyncio.run(main())
```

`sync_read_ai(source, *, dataset_name, user=None)` is **required** for the supported lifecycle: authorization, full-run locking, explicit merge, recovery, cognification, cleanup verification and successful checkpoints. It returns `SyncResult` counts and the current Data IDs. Connector validation failures raise `ReadAIError` subclasses; Cognee storage or model errors propagate with the unfinished attempt retained.

`read_ai_source(connection_id, *, credentials_path=None, token_provider=None, meeting_ids=None, start_time=None, end_time=None, include_summary=True, include_transcript=True, include_metrics=True, client=None, state_dir=None, limits=None)` is lazy and performs no request at construction. Choose explicit `meeting_ids` **or** timezone-aware date bounds. The start is inclusive; the end is exclusive. Empty explicit-ID selections, conflicting modes, naive dates and selecting no content are rejected. All content flags default to true. Configuration includes `end_time=None`, not the upper bound frozen separately for each run. Changing selection under an existing connection ID fails: use a new ID and retire the previous connection when appropriate.

An injected token provider implements `get_tokens(*, force_refresh=False) -> TokenBundle`; it supplies client/ID-token proof for initial binding, not an arbitrary account subject. Import `TokenBundle` from the package's `auth` module. `client` accepts an HTTPX client for transport integration and tests.

### Lower-level dlt integration

Direct use of `read_ai_source` with `cognee.add` is an integration interface; it does not supply the complete acknowledged synchronization contract. Direct callers must manage authorization, identity, locks, recovery and verification, and explicitly pass:

```python
await cognee.add(
    source,
    dataset_name="read-meetings",
    dlt_config={"write_disposition": "merge", "primary_key": "id"},
    run_in_background=False,
)
```

Declaring merge on the dlt resource alone is insufficient because Cognee's resolver can otherwise override it with replace. The resource includes the actual dlt `_deleted` column hint `{"data_type": "bool", "hard_delete": True}`; normal rows use false and confirmed-removal tombstones use true.

## Content and removal policy

Each meeting produces one deterministic document: summary, meeting context, selected metrics, then a speaker-attributed transcript. Meeting timestamps use UTC and transcript turns preserve relative offsets. The report link and participant names are included; recordings, temporary download URLs and participant email fields are excluded. Free text supplied by the provider is not redacted automatically.

| Selected section | Current available content | Missing, null or empty |
|---|---|---|
| Summary | Nonempty string | Withdraw previous summary; mark unavailable |
| Transcript | Valid nonempty turns, otherwise nonempty text | Withdraw previous transcript; mark unavailable |
| Metrics | Current metric object, including zero values | Withdraw previous metrics; mark unavailable |

The returned metric object replaces the old projection; absent/null nested metrics disappear. Malformed values abort extraction instead of clearing content. If no selected section is available, withdraw the prior document but keep the meeting in reconciliation state so later enrichment can restore it. Active meetings and unavailable sections remain pending and are revisited every run. These are **connector policies**; unavailable content is not proof that the provider intentionally cleared a field.

Within the **same sync attempt**, a complete inventory plus a detail `404` and second complete inventory confirms loss of visibility. Explicit IDs use two detail requests. Confirmed removal withdraws its owned document and provenance, including the final document in a connection. Read.ai distinguishes owner **Delete report** from another user's **Remove report**; documentation does not specify every API transition. [Deletion behavior](https://support.read.ai/hc/en-us/articles/22408013818899-How-do-I-delete-a-meeting-report)

A missing but still-retrievable report, `403`, authentication failure, invalid page or exhausted retry makes synchronization incomplete. Such failures do not authorize deletions or advance success. **This is not complete access-control synchronization:** previously imported content can remain locally available while access is uncertain. Resolve permissions or deliberately retire the local connection; never interpret `403` as deletion.

## Bounds, durability and retirement

`SyncLimits` defaults are 1,000 distinct meetings, 300 inventory-page requests, 4 MiB per decoded API response, 128 MiB total decoded API responses and a 60-minute run deadline. Exceeding a limit fails instead of truncating the corpus. The shared rate budget remains below Read.ai's documented 100 requests/minute/user. HTTP timeouts and retries are bounded; OAuth requests have their own 30-second timeout and 1 MiB cap.

```python
from cognee_community_connector_read_ai import SyncLimits

source = read_ai_source(
    connection_id="my-read-account",
    limits=SyncLimits(max_meetings=2000, max_pages=500),
)
```

Private synchronization state defaults to `~/.local/state/cognee-read-ai`; override `state_dir` or `READ_AI_STATE_DIR`. **Keep tokens and state outside the checkout. State contains selected meeting content** and must be protected like the resulting Cognee corpus. Do not casually delete it to resolve a failed run.

The account rate counter is shared across connections and Cognee installations under `$XDG_STATE_HOME/cognee-read-ai/rates` (default `~/.local/state/cognee-read-ai/rates`). Set `READ_AI_RATE_DIR` only to another private location shared by all local connections for that account. Connection state directories cannot split the shared quota.

A process lock covers authorization through final verification. The last successful generation remains distinct from dlt's loaded state: validate upstream → persist an attempt → load staging → cognify → verify → acknowledge. Unchanged-run recovery uses Cognee's retained-table readback. Pending dlt packages are aborted only in the connection-owned pipeline and its staging table is rebuilt from newly validated upstream data; abort is not destination rollback. Interrupted rebuilding stays pending. Cached content is never sufficient authority for replay after permission or account changes. The deadline prevents successful acknowledgement after its limit; it cannot forcibly stop a Cognee worker thread. A storage phase can therefore extend past the deadline while the connector waits for it to settle. Cancellation keeps the lock until active storage work stops.

To replace a selection, first stop the old connection's scheduled runs. To remove its local imported corpus without deleting upstream reports:

```python
from cognee_community_connector_read_ai import retire_read_ai

await retire_read_ai(old_source, dataset_name="read-meetings")
```

Retirement removes only the old connection's recorded Data IDs through Cognee's deletion API, empties only its staging resource, verifies cleanup, then marks it retired. It does not reset the shared dataset or other connectors. Backend verification is isolated in a small compatibility module. **Known Cognee 1.6.4 dependency:** if deleting an orphan `EdgeType_relationship_name` vector fails after its graph node is removed, core logs the failure and loses the graph evidence needed to retry that vector deletion. The real-backend fault test reproduces a residual vector after a single injected failure and another unchanged sync. The connector detects it, raises `VerificationError`, and keeps the last-successful checkpoint unchanged; it cannot automatically repair this condition. Recovery needs a core cleanup change or an independently verified operator repair. Do not reset the journal or treat this failure as a successful removal. No connector-specific backend deletion workaround is included.

## Examples and verification boundaries

```bash
uv run python examples/example.py --help
uv run python examples/offline.py
uv run pytest tests/ -q
uv run ruff check .
uv run ruff format --check .
uv build
```

The live example accepts a connection ID, dataset, credential path and selection, runs the required synchronization entrypoint, then optionally performs dataset-scoped graph-completion search. It does not prune existing Cognee data. The offline example uses synthetic Read.ai/model responses and temporary real dlt/SQLite/Ladybug/LanceDB storage.

Offline tests exercise signed OAuth validation, shared-token refresh, paging, sections, bounded failures, changed-only processing, scoped deletion and recovery. Integration claims require actual Cognee ingestion, cognification, graph/vector retrieval and cleanup assertions—not source-row mocks. The dedicated workflow covers Python 3.11–3.13, lint/format, package build and installed-wheel imports. Recorded local results belong in `VERIFICATION.md`.

**Live provider acceptance is not verified.** Before declaring the issue completely verified, use authorized synthetic meetings to record OAuth setup/refresh, historical edits, missing/null/empty enrichment behavior, owner deletion, user-list removal and permission transitions, then inspect Cognee document/chunk/vector cleanup. Offline tests cannot establish undocumented provider behavior or its deletion timing.
