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
    prometheus_source("http://localhost:9090"),  # or PROMETHEUS_URL from env
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
| `rule_type` | both | `"alert"` or `"record"` to read one kind of rule. |
| `rule_groups` | all groups | Read only these rule groups. |
| `include_metadata` | `False` | Also read type, unit and help text for every metric the server knows. |

See `examples/example.py` for the full flow.

## What is ingested

| Staging table | One document per | Source |
|---|---|---|
| `prometheus_rules` | alerting or recording rule | `GET /api/v1/rules` |
| `prometheus_metrics` | metric, only with `include_metadata=True` | `GET /api/v1/metadata` |

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
  rule.
- A rule removed from the server, or a whole rule file, drops out of the snapshot, and cognee's
  `orphan_cleanup` forgets it.
- An HTTP error, a body that is not JSON, or a `status` other than `success` aborts the run and
  leaves memory untouched. A partial snapshot would otherwise be forgotten as if it were a
  deletion. A server that answers with an empty rule list does empty memory.

Leave `write_disposition` at its default. Passing `merge` breaks forget-on-delete for a snapshot
source.

## Limits

- **Prometheus itself.** Tested against Prometheus 3.15.0 run locally, and read-only against
  the public demo server (3.13.0). Mimir, Thanos and VictoriaMetrics serve a compatible rules API
  with their own differences. They were not tested.
- **One dataset per server.** Each sync is a full snapshot of one server.
- **Metadata can be large.** A bare Prometheus 3.15.0 reported 265 metrics, and a real server can
  report thousands, each one a document to cognify. That is why it is off by default.
- **dlt keeps its load files.** Each sync leaves a gzip copy of the loaded rows in dlt's pipeline
  directory, and forgetting a document does not remove those copies. Set
  `LOAD__DELETE_COMPLETED_JOBS=true` to have dlt delete them after each load.

## Setup

1. Find the server URL. If Prometheus runs with `--web.route-prefix=/prom`, the URL includes
   `/prom`.
2. Set it as `PROMETHEUS_URL` (or pass `base_url=...`), plus credentials if a proxy needs them,
   and your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest pytest tests/
```

The tests run offline. A fake `requests` transport stands in for the server, so the real
`rest_api` source and auth run against responses recorded from a local Prometheus 3.15.0
(`tests/fixtures`, with the rule file paths shortened). They cover the definition-only rows, ids
for repeated rule names, edits and removals on re-sync, path prefix and auth, and aborting on a
failed or malformed read. One test runs `cognee.add()` and `cognify()` with the LLM and embeddings
mocked and checks that a removed rule leaves the graph.
