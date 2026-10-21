# Grafana connector for Cognee

Sync selected Grafana dashboard definitions, nested panel titles/descriptions and stored
human annotations into searchable Cognee documents. Dashboard versions avoid re-rendering
unchanged definitions; annotations are rescanned independently. A verified full snapshot
replaces staging, so removed content is forgotten on the next foreground sync, including
the final document. Queries and metric values are never executed or ingested.

## Supported contract and permissions

Initial support is **Grafana OSS 12.3.1**, organization **1**, namespace **default**, using
`/apis/dashboard.grafana.app/v1beta1/namespaces/default/dashboards` with classic dashboard
schemaVersion 42, plus `/api/annotations` backed by Grafana SQL storage. There is no silent
legacy/v1/v2 fallback. Cloud, other organizations/schemas, external annotation stores and
library panels require separate validation and are unsupported. Unsupported selected
library panels or conversion failures abort the snapshot rather than omit context.

Use a dedicated service-account token. Extraction verifies `/api/user.uid`, organization
and effective permissions before and after reading. It also reads the current stored
service-account role to catch cached grants after a role downgrade. Safe automatic reconciliation requires
organization-wide `dashboards:read` on `dashboards:*`, `folders:read` on `folders:*` and
`annotations:read` on `annotations:*`. **On the tested OSS release these grants were
verified with an organization Admin service account, which this connector requires.** This role also permits provider
writes, although the connector issues GET requests only. Store the token securely, use a
disposable instance first, and revoke it when no longer needed. A restricted Viewer account
is refused: HTTP 200 filtered lists cannot reliably distinguish deletion from access loss.
Equivalent custom read-only roles on Enterprise have not been tested.

In Grafana, open Administration → Users and access → Service accounts, create the dedicated
account in organization 1, assign its role and create a token. Never paste the token into
source code or a chat. Set it through your local secret store/environment.

## Install and run

Python 3.11–3.13 is declared. Cognee 1.6.5 and dlt 1.30.0 are the initial validation targets;
other allowed dependency versions are not individually certified. From this package:

```sh
uv sync --extra test --locked
# Configure your preferred Cognee extraction and embedding providers first.
export GRAFANA_URL='https://grafana.example.com'
# Set GRAFANA_SERVICE_ACCOUNT_TOKEN securely in your shell/secret store.
uv run --locked python examples/quickstart.py --owner my-user --resource incidents \
  --dataset grafana_incidents --from-ms 1767225600000 --dashboard dashboard-uid
```

For a local disposable OSS server, bind its port to loopback and run the pinned
`grafana/grafana:12.3.1` image with credentials supplied privately. Only localhost accepts
HTTP; remote URLs require HTTPS. Grafana behind a URL subpath is supported by the client.
This example reads selected prose and sends it to your configured Cognee model providers.
Choose local providers if that content must remain on your machine.

The public factory does no network work until extraction:

```python
from cognee_community_connector_grafana import grafana_source, SyncStats

stats = SyncStats()
source = grafana_source(
    base_url=url,
    token=token,
    org_id=1,
    owner_id="my-user",
    resource_name="incidents",
    folder_uids=["operations-folder"],
    organization_annotation_tags=["incident", "production"],
    annotation_from_ms=1767225600000,
    stats=stats,
)
await cognee.add(
    source,
    dataset_name="grafana_incidents",
    run_in_background=False,
    dlt_config={"primary_key": "id", "write_disposition": "replace"},
)
await cognee.cognify(datasets=["grafana_incidents"])
```

Dashboard UID and folder selections are a union. Organization annotation tag filters use
case-sensitive keys and optional values: `region` (or `region:`) matches that key with
any value; `region:EU-WEST-1` requires the exact value. ASCII spaces around keys/values
are trimmed and equivalent filters deduplicated. Empty keys and filters with more than
one colon are refused instead of accepting Grafana's silent truncation. Each supplied
filter must match a returned annotation. Grafana's SQL endpoint combines filters using
matching-row counts; overlapping filters/multiple values for one key can behave differently
from logical AND. Prefer distinct tag keys. A provider response missing an intended
predicate aborts the snapshot. Matching dashboard-linked annotations are excluded from
that organization selection. All selected dashboard annotations are included within the
configured history.
Folder context contains its stable UID, not a separately changing folder title. Dashboard
annotation-query configuration is not a stored annotation event and is not executed.

## Identity, history and deletion

`owner_id` and a required, distinct `resource_name` identify **one corpus**. Use the same
values for reruns; give independent sources different resource names, even in the same
dataset. They determine the stable staging table. Actual dataset pipeline state is also
isolated by Cognee. Reusing a resource with a different instance, organization, namespace
or service account is refused. Rotating a token on the same account preserves identities.
Do not run the same resource/dataset concurrently or override its replace disposition.

Dashboard identity is its route UID (`metadata.name`, not resource UUID); annotation
identity is instance/organization plus annotation ID. Rename, folder move and annotation
linkage changes update content without minting another provider ID. Cognee itself uses
content-versioned document identities to replace stale text.

Each sync reads complete dashboard pages, rescans annotation history twice and verifies
selected dashboard inventory again. Revisions are opaque equality markers; on this target
`metadata.resourceVersion` represents dashboard version, while `spec.version` is absent.
The full unchanged-row cache is re-emitted under replacement. New/changed documents flow
through Cognee; unchanged rows are retained. This is incremental rendering/memory
processing, **not** a changed-only network feed or a transactionally frozen provider read.

Set a **strictly positive** `annotation_from_ms`. The default upper bound is the current
time; an explicit `annotation_to_ms` is allowed, but cannot move backwards across runs.
Old edits/removals are reconciled only inside that history. Lower-bound or selection
changes deliberately evict excluded content from this same corpus; they are policy
changes, not evidence of upstream deletion. To retire a corpus, pass an explicit empty
selection (or use the example's `--retire`); its table stays the same. Do not change the
resource name merely to alter selection, as that would leave the old corpus separate.

Annotation limit is a cap, not pagination. Saturated windows are subdivided, overlapping
regions are deduplicated by ID, and one-millisecond saturation aborts rather than silently
truncate. The request budget is shared by both scans and inventory checks. Errors, denied
permissions, account changes, invalid schema, changing inventories or caps withhold the
whole snapshot. This assumes the pinned SQL provider honors the requested positive
bounds and cap. Grafana's own retention may remove old annotations; those absent from an
otherwise authoritative covered history also leave the selected corpus.

## Recovery and verification

Retry a failed extraction after resolving its reported category; prior staging remains.
DLT may retain a normalized load package after a destination failure. A normal rerun
replays that package before accepting fresh input; run sync again afterward to fetch the
latest provider snapshot. Do not delete pipeline state or reuse it for a different account.
If Cognee fails after staging, retry the same source: complete retained snapshots allow
reconciliation even when the provider has not changed.

Use foreground ingestion. Cognee cleanup can log an orphan failure without raising;
inspect its logs and verify obsolete Data, graph and vector records are absent. Re-running
allows cleanup to retry. A successful return or a missing search hit alone is insufficient
proof of deletion. `SyncStats` reports logical reads, selected dashboard/annotation counts,
changed/unchanged rows and corpus removals, plus failed extractions; it does not certify
that downstream graph cleanup completed. Physical HTTP attempts can exceed logical reads
because each read has up to three attempts; owned clients allow five seconds of total
retry waiting with cancellation checkpoints.

To reproduce a manual lifecycle, run the example, search for a unique synthetic panel and
annotation phrase, rerun unchanged, edit/remove the panel and independently edit/add/delete
annotations, then delete the last selected dashboard and rerun. Confirm obsolete text is
absent from each store. The example makes no provider writes or global prune calls.

## Privacy and tests

Only allowlisted titles, descriptions, tags, IDs, times and provenance are rendered.
Datasource settings, targets, variable values, raw options and secure config are omitted.
The supplied token, explicit `redact_values` (four characters or longer), recognizable
credential shapes, URL credentials and labelled secrets are filtered from rendered prose
on a best-effort basis. Labelled secrets include API-token fields; authorization header
credentials and parameter lists are filtered while following incident clauses are kept.
URI userinfo, including password-only forms, is filtered for database/driver schemes
as well as HTTP(S). Ordinary dates, regions, response codes and generic query values
are preserved. Unknown/very short credentials and secrets pasted into arbitrary prose
cannot be guaranteed detectable. Review the selected content and use explicit redactions
for known secrets. Never log full provider payloads or credential files.

```sh
uv run --locked pytest tests -q
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Tests use fake Grafana responses with real temporary dlt/SQLite storage and exercise
factory defaults, revisions, independent annotations, final deletion, failures, replay,
selection/account/resource/dataset isolation, capped intervals and prose filtering.
Authenticated provider and model/store validation must be reported separately from these
offline tests; passing them does not certify untested Grafana versions or backends.
