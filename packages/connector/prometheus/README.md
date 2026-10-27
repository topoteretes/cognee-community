# cognee-community-connector-prometheus

A Prometheus data-source connector for [cognee](https://github.com/topoteretes/cognee). It syncs
a server's alerting and recording rules into memory, so an agent can answer "what does this
alert check, who does it page, and is there a runbook". Metric help text can be added as well.

It exposes a `dlt` source, built on dlt's declarative `rest_api` source, that you hand to
`cognee.remember(...)` / `cognee.add(...)`. Rows are ingested as normal documents through
cognee's document-mode marker. No samples are read and no PromQL is run.

## Install

```bash
uv pip install cognee-community-connector-prometheus
# or, from this monorepo:
cd packages/connector/prometheus && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_prometheus import prometheus_source

await cognee.remember(
    prometheus_source(base_url="http://localhost:9090"),  # or PROMETHEUS_URL from env
    dataset_name="prometheus",  # give the connector its own dataset
    self_improvement=False,  # skip the enrichment pass on repeated syncs
)

answer = await cognee.search(
    query_text="Which alerts page the payments team, and what is the runbook?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["prometheus"],
)
```

| Argument | Default | What it does |
|---|---|---|
| `base_url` | `PROMETHEUS_URL` | Server URL, including any `--web.route-prefix` path. |
| `username`, `password` | `PROMETHEUS_USERNAME`, `PROMETHEUS_PASSWORD` | Basic auth. |
| `token` | `PROMETHEUS_TOKEN` | Bearer token, for a proxy that expects one. Use a token or basic auth, not both. |
| `headers` | none | Extra headers sent with every request. |
| `rule_type` | both | `"alert"` or `"record"` to read one kind of rule. Also applied after reading, for a server that ignores it. |
| `rule_groups` | all groups | A list of rule groups to read. Also applied after reading. Without `rule_type`, a listed group the server does not have stops the run. |
| `include_metadata` | `False` | Also read type, unit and help text for every metric the server knows. |
| `allow_empty` | `False` | Let one sync through that finds no rules although an earlier sync found some, or that lacks groups of `rule_groups`. Their documents are forgotten. Missing groups are remembered, so later runs skip them without the flag, until they are back. Each run warns how many it skipped, with the time of the run that accepted them, and lists them in `cognee_sync_stats["skipped_groups"]`. |
| `allow_empty_metrics` | `False` | Let one sync through whose metric metadata has under half the metrics of the last sync. Separate from `allow_empty`, so forgetting rules never forgets metrics by accident. |
| `resource_name` | from the address, e.g. `prometheus_localhost_9090_<hash>` | Prefix of the staging tables. Each server gets its own by default; the hash of the exact address keeps `prom-eu` and `prom.eu` apart. |
| `check_active` | none | Called before every response is used. Raise from it to stop a sync whose access was revoked. |
| `service` | dlt's session, kept short | `requests.Session` to send requests through. By default a 429 stops the run with memory unchanged, because waiting would hold cognee's staging lock and the next run re-reads everything. A 5xx or a dropped connection gets one retry after at most 5 s. Each request times out after 30 s. `session` is accepted as another name for it. |

All arguments are keyword-only. After a run, `source.cognee_sync_stats` holds the row count of
each table (`rules`, and `metrics` with metadata on) and `failed_rate_limit`, the number of 429
answers that stopped the run, plus `skipped_groups`, the `rule_groups` entries a run skipped because
an earlier `allow_empty=True` run accepted them as gone. The counts are cleared when a run starts
and logged when it ends. A table that empties after holding rows logs a warning.

See `examples/example.py` for the full flow.

## What is ingested

| Staging table | One document per | Source |
|---|---|---|
| `<resource_name>_rules` | alerting or recording rule | `GET /api/v1/rules` |
| `<resource_name>_metrics` | metric, only with `include_metadata=True` | `GET /api/v1/metadata` |

A rule document holds the rule's definition: type, name, group, file, evaluation interval,
expression, `for`, `keep_firing_for`, labels and annotations. Annotations are stored as written,
so `{{ $labels.instance }}` stays a template.

Runtime state is left out: `state`, `health`, `lastError`, evaluation times and the active
alerts. They change between two reads of an unchanged server. The request sets
`exclude_alerts=true`, and each row is rebuilt from the definition fields, so a server that
ignores the parameter makes no difference.

## How sync and forget-on-delete work

Prometheus has no change feed for rules, but the whole rule list is one request. Each run is a
**full snapshot**, loaded with `write_disposition="replace"`.

- Unchanged rules keep a stable content-hash id, so they are not re-ingested or re-cognified.
- An edited rule (expression, `for`, annotations) is re-read, and only its document is processed
  again.
- A rule's identity is its file, group, type, name and labels. Two rules with the same name in one
  group, such as one per severity, are separate documents. Changing a rule's labels makes it a new
  rule, and so does moving its rule file to another path. Group-level `labels` arrive merged into
  each rule's labels, so they count too.
- A rule removed from the server, or a whole rule file, drops out of the snapshot, and cognee's
  `orphan_cleanup` forgets it.
- An HTTP error, a body that is not JSON, a `status` other than `success`, or a response that
  carries `warnings` aborts the run and leaves memory untouched. Thanos Querier answers `success`
  with a warning and only the reachable rulers' groups when one is down. A partial snapshot would
  otherwise be forgotten as if it were a deletion.
- A server that suddenly has no rules at all, after an earlier sync found some, aborts the run too.
  Rule files that did not load, or a filter that matches nothing, look exactly like that. The
  check counts the rules left after `rule_type` and `rule_groups`, so a server that ignores the
  filters is covered too. A group named in `rule_groups` that is gone, renamed or failed to load
  stops the run as well, while the other groups would still load. Pass `allow_empty=True` once to
  forget rules that really were removed.

Leave `write_disposition` at its default. Passing `merge` breaks forget-on-delete for a snapshot
source.

## Limits

- **Prometheus itself.** Tested against Prometheus 3.15.0 run locally, and read-only against
  the public demo server (3.13.0). Mimir, Thanos and VictoriaMetrics serve a compatible rules API
  with their own differences. They were not tested.
- **Servers can share a dataset.** Each server gets its own tables, named from its address, so
  syncing two servers into one dataset keeps both. The name follows the address, so if a server
  moves, pass the old `resource_name` to keep its tables. Two different filters on one server share
  tables, so give each its own `resource_name` or dataset.
- **Renaming leaves the old documents.** A new `resource_name` (or a new address, when it is
  derived), or switching `include_metadata` off, stops writing to the old tables but does not
  forget their documents. To start clean, run `await cognee.forget(dataset="<your dataset>")`
  before the next sync.
- **Who can read it.** Rule expressions, labels and annotations, runbook links included, land in the
  dataset. Anyone with read access to the dataset in cognee can read them.
- **Metadata can be large.** A bare Prometheus 3.15.0 reported 265 metrics, and a real server can
  report thousands, each one a document to cognify. That is why it is off by default.
- **Metadata needs live targets.** Prometheus builds it from its scrape caches, so right after a
  restart it is empty, then fills in target by target over a scrape interval. A sync whose
  metadata has under half the metrics of the last one is refused and fails, instead of forgetting
  the rest. Run it again once the scrapes are done, or pass `allow_empty_metrics=True` if the
  metrics really went away. A smaller drop goes through: metrics of targets that left are
  forgotten, and come back as new documents when the targets return. A server that never had
  targets just syncs its rules with no metrics.
- **dlt keeps its load files.** Each sync leaves a gzip copy of the loaded rows in dlt's pipeline
  directory, and forgetting a document does not remove those copies. Set
  `LOAD__DELETE_COMPLETED_JOBS=true` to have dlt delete them after each load.
- **A private CA.** For a server with a certificate from an internal CA, point
  `REQUESTS_CA_BUNDLE` at the CA bundle. Credentials go in `username`/`password` or `token`, never
  in the URL, where they would end up in logs.

## If you synced with an earlier version

Nothing has been released yet, but if you ran this connector from an earlier commit of its PR:

- Default table names now end in a hash of the server's address
  (`prometheus_localhost_9090_<hash>_rules`), so the documents of an earlier sync stay next to the
  new ones. Clear them with `await cognee.forget(dataset="<your dataset>")` before syncing, or pass
  the old name as `resource_name`.
- `resource_name` is checked more strictly: `__` and a trailing `_` are rejected with a
  `ValueError`.

## Setup

1. Find the server URL. If Prometheus runs with `--web.route-prefix=/prom`, the URL includes
   `/prom`.
2. Set it as `PROMETHEUS_URL` (or pass `base_url=...`), plus credentials if a proxy needs them,
   and your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest pytest tests/
```

CI (`.github/workflows/prometheus_connector_tests.yml`) runs the suite on the versions in
`uv.lock` with Python 3.10 and 3.14, and on the floor in `pyproject.toml` (cognee 1.6.1, dlt 1.9.0).

The tests run offline. A fake `requests` transport stands in for the server, so the real
`rest_api` source and auth run against responses recorded from a local Prometheus 3.15.0
(`tests/fixtures`, with the rule file paths shortened). They cover the definition-only rows, ids
for repeated rule names, edits and removals on re-sync, path prefix and auth, and aborting on a
failed or malformed read. One test runs `cognee.add()` and `cognify()` with the LLM and embeddings
mocked and checks that a removed rule leaves the graph, and another runs two servers through
`cognee.add()` into one dataset.
