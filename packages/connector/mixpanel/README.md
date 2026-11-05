# Mixpanel definition connector

Sync registered event schemas, saved report query definitions and cohort rules into Cognee.
Raw events, profile records, cohort members and calculated report results are excluded.

## Installation

```sh
pip install ./packages/connector/mixpanel
```

Python 3.11–3.13. The initial compatibility target is Cognee 1.6.5, dlt 1.30.x and
Mixpanel Headless 0.4.0. Headless is alpha: the version is pinned because this adapter
uses its internal App API client and raw response envelope option.

## Test project and authentication

Create a dedicated Mixpanel test project. Add a documented event schema, a saved report
and a cohort with actual rules. Create a service account with access to the project
and selected workspace. Creating service accounts requires an organization/project
owner or admin; runtime read permissions should be limited to the selected content.
The project must also support saving cohorts. A free EU test project rejected cohort
creation with HTTP 401 and `plan does not allow saving cohorts` during live validation;
successful report/schema access does not establish cohort availability on that plan.

Copy `.env.example` to `.env` locally and fill in:

```dotenv
MIXPANEL_PROJECT_ID=12345
MIXPANEL_SERVICE_ACCOUNT_USERNAME=service-account-username
MIXPANEL_SERVICE_ACCOUNT_SECRET=service-account-secret
MIXPANEL_REGION=us
# Optional: explicit workspace/data-view ID, verified against project metadata.
MIXPANEL_WORKSPACE_ID=67890
```

Keep `.env` out of Git. The connector builds an in-memory SDK Session and does not read
ambient Headless accounts or persist a login. Regions: `us`, `eu`, `in`. A project API
secret is **not** supported as a substitute for service-account credentials on these
App/Lexicon routes. No browser OAuth flow is needed.

Run the read-only validation before ingesting:

```sh
python packages/connector/mixpanel/examples/validate_access.py
# Prefer explicit selections in a project containing other definitions:
python packages/connector/mixpanel/examples/validate_access.py \
  --report-id 101 --cohort-id 202 --schema-name Signup
```

It validates the same two-inventory capture and rendering path as ingestion, without
staging or printing credentials, provider bodies or contact information. It neither
creates nor deletes Mixpanel objects. Visible project/workspace presence is checked;
the fingerprint records any returned access fields, not a universal ACL guarantee.

## Selected ingestion

```python
from cognee_community_connector_mixpanel import mixpanel_source, sync_mixpanel

source = mixpanel_source(
    resources=("schema", "report", "cohort"),
    report_ids=[101],
    cohort_ids=[202],
    schema_names=["Signup"],
    workspace_id=67890,
)
dataset_name = await sync_mixpanel(source)
```

`None` selection means all visible objects of that enabled kind; `[]` means none.
An explicitly selected ID/name that has never been seen in this scope fails, catching
typos and inaccessible selections. Previously seen selected objects may be reconciled
when absent; their known identities remain in the checkpoint for subsequent runs.
Schema names preserve case and spacing. Report types may also be filtered with
`report_types=["funnels", "insights"]`. The default limit is 25 selected objects and
2 MB of rendered content. A selection that exceeds limits fails instead of truncating.

Use `sync_mixpanel()` for ingestion. It derives a dataset from region, project, workspace
and service-account principal, serializes the sync, and explicitly calls
`cognee.add(..., primary_key="id", write_disposition="merge")`. Cognee's default
`replace` would discard unchanged records from an incremental batch. `cognify=False`
stages/adds documents only; the default also runs `cognee.cognify()`.

For a complete ingest/search example:

```sh
python packages/connector/mixpanel/examples/example.py
```

Cognee needs its usual LLM/embedding configuration for the live cognify/search example.
The connector does not purchase provider access or create a scheduled job.

## API scope and data handling

| Kind | Provider read | Definition content |
| --- | --- | --- |
| Event schema | Project-scoped Lexicon `/projects/{id}/schemas` | `schemaJson` property map, descriptions, schema constraints, approved display/tags/hidden/drop metadata |
| Saved report | App API `bookmarks`, `v=2`, plus detail GET | Name, report type, approved query parameters, ordered steps and filters |
| Cohort | App API `cohorts`, plus detail GET | Supported `selector`/`behaviors` or `groups` predicate trees |

Report/cohort reads use the explicitly selected workspace when configured. Lexicon
always uses the documented project-scoped route. Only registered schemas appear;
this is not a promise to enumerate every event visible in the Lexicon UI.

Documents preserve structured definitions instead of inventing prose explanations.
Saved-metric/behavior/custom-event IDs remain unexpanded references. Unsupported root
report fields, missing predicates and malformed inventories stop the batch and require
a renderer update backed by provider fixtures. Creator/contact fields and sampled
values are omitted or marked as omitted. Recognized sensitive filter literals/email addresses are redacted
by default; `include_sensitive_literals=True` permits literals in approved semantic
fields. This is a conservative field policy, not a guarantee that arbitrary descriptions
contain no personal information. Review the selected project content before ingesting.
Opaque selector strings mentioning identifying properties are marked as redacted;
their complete rule is available only with explicit literal inclusion. Redaction can
make different identifying literals produce the same semantic hash, intentionally.

## Incremental sync, deletion and recovery

The APIs do not expose a verified universal date-window cursor for definition changes.
Each sync reads fresh selected inventories, fetches details, then repeats inventories
and access metadata. New/changed semantic document hashes are merged; unchanged rows
remain staged. The checkpoint is a committed ID-to-hash inventory, not an event-export
timestamp. Dynamic view/member counts and polling timestamps never enter documents.

After two consistent inventories and unchanged access metadata, objects no longer in
the selected visible corpus receive dlt hard-delete tombstones. This includes objects
removed from local selection. **Disappearance reconciles visibility; it does not prove
physical deletion.** A provider may hide objects after individual sharing changes that
project-level metadata does not expose. Use a dedicated service-account scope, and
validate its visibility/deletion contract on your tenant before relying on automatic
forgetting. Explicitly forbidden objects, permission errors, incomplete inventories or
changed access metadata abort the run; they are not converted to deletion events.

All required reads/rendering finish before any row is yielded. Unexpected paging or
truncation metadata aborts: these collection routes have no verified continuation
contract in this adapter. Concurrent edits detectable by inventory metadata stop the
sync; two reads do not make an atomic Mixpanel snapshot.

Cognee 1.6.5 reads back the complete retained staging corpus and handles empty tables
for document-source cleanup. A failed downstream stage can be retried with a fresh
source and an unchanged upstream definition. Staging, document addition, graph/vector
cleanup and cognify are separate stages. Check errors/logs: successful staging alone
does not establish successful graph/vector deletion.

Selection changes retain state so removed selections are reconciled. Changing principal,
project or workspace creates a separate dataset; retire the old dataset explicitly.
Rotating a secret for the same username preserves identity.
The source checkpoint rejects reuse of the same manual dlt pipeline for a different
access scope. An injected test/client adapter owns its authentication, scope and state
directory; combining it with competing configuration fails. Cancellation waits for the
in-flight add/cognify task to settle before releasing the scope lock, so it may not be
immediate. A terminated process relies on the next sync's retained-staging replay.

## Quotas and local state

Headless documents a default 60 requests/hour. The client allows at most 50 outbound
attempts per sync by default and a persistent 60/hour budget per region/principal.
Retries count too. Retry count is bounded; a long Retry-After stops the run for a later
retry. Responses are bounded to 2 MB. Detail reads make small selections preferable.
The documented five-per-minute Lexicon limit is also enforced persistently. These are
conservative local budgets; the account's actual provider limits remain authoritative.

The default state directory is `~/.cache/cognee-mixpanel`; it holds a private request
budget SQLite database and sync locks, not credentials or provider bodies. Pass
`state_directory=` to change it. Budget state must be shared if multiple processes
use the same account. The sync helper requires a POSIX host (macOS/Linux) for file locks.

If separate runtimes use the same Mixpanel scope with different Cognee database roots,
set a distinct `DLT_DATA_DIR` for each runtime before importing/configuring Cognee.
Otherwise dlt's default global pipeline directory can mix their local checkpoints.
Keep the connector's request-budget state shared across those runtimes; isolating dlt
working directories does not give the service account additional request quota.

## Development and validation status

```sh
uv sync --group dev
uv run pytest tests -q
uv run ruff check .
uv run ruff format --check .
```

The package suite has 59 passing tests, including sanitized saved-funnel and event-schema
responses from a dedicated EU synthetic project. Offline tests isolate Cognee's environment
file so local model credentials do not override the mocked model configuration.

Live service-account/project/workspace reads and rendering passed for two registered event
schemas and one saved funnel. The report's baseline, unchanged run, seven-to-three-day
semantic edit, reset, deletion and recreation passed with Azure GPT extraction, local
Fastembed embeddings and real SQLite/Ladybug/LanceDB stores. Public CHUNKS search returned
the current definition. Updates removed old document references and query rules; deletion
left zero Data records, graph nodes, edges and vector chunks. Recreating the report with
a new provider ID restored the original seven-day definition as a searchable document.
The individual report DELETE route initially returned HTTP 500; the SDK's bulk-delete
route succeeded when restricted to that one synthetic report.

The two registered schemas passed live ingestion, unchanged sync, description update,
single-schema deletion/restoration and final deletion/restoration of both selected
schemas. Unchanged Purchase retained its Data ID during the Signup update; both original
IDs returned on restoration. The empty selected corpus left zero Data records, graph
nodes, edges and vector chunks, with no stale definition in public search.
A live invalid-credential request returned HTTP 401 and left the existing report's
Data, graph, vector and search observations unchanged. This checks authentication
failure; it does not establish per-object sharing or permission-loss behavior.

The selected synthetic definitions also passed rendered UI comparison: the board
shows one report card; its ordered Signup/Purchase steps, seven-day conversion
window, `plan = test` filter and 30-day date range match the saved query. Both
events' names, descriptions, visibility and tags, and their `plan`/`test_run`
property names, String types and descriptions match the registered schemas.
Fresh API readback after the UI inspection matched all three retained Cognee
documents. This verifies these selected definitions, not every report structure
or tenant inventory.

Live cross-project isolation also passed in a second authorized EU Free project
(`4081075`): baseline ingestion, deletion to zero Data/graph/vector content and
restoration of the same schema Data ID, using the same real Cognee stores.
The original project's report observations stayed unchanged at every stage.
The new project also rejects saved cohorts and requires Growth for granular
board sharing. Creating another Free project did not unlock those live gates.

Per-object permission-loss/sharing behavior and live cohort predicates remain
validation gates. The Share dialog requires the Growth plan to control who can
view/edit boards; project-wide Editor access and public sharing off were observed,
with no sharing settings changed. The current test-project plan rejected
cohort creation again with `plan does not allow saving cohorts`; no trial or upgrade
was enabled. Successful report/schema lifecycle checks do not establish cohort support.
The Cognee integration test uses real SQLite, Ladybug and LanceDB, mocked LLM output,
and deterministic local token embeddings. It checks public chunk search, updates,
final deletion, restoration, cross-project isolation and unchanged-data replay after
downstream failure. It does not assess live embedding quality or analytical correctness.

Sources: [issue #4777](https://github.com/topoteretes/cognee/issues/4777),
[Headless](https://docs.mixpanel.com/docs/mixpanel-headless),
[service accounts](https://docs.mixpanel.com/reference/service-accounts),
[Lexicon schema scope](https://docs.mixpanel.com/reference/lexicon-schemas-api),
[schema API](https://docs.mixpanel.com/reference/list-all-schemas-for-project).
