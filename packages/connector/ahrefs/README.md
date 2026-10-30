# cognee-community-connector-ahrefs

An Ahrefs data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync Domain Rating history, backlinks, keyword rankings and Site Audit issues into memory
as dated rows, and forget them when they are deleted upstream.

It exposes `dlt` sources you hand to `cognee.remember(...)`, one per report. Rows are SEO
metrics, not prose, so they take cognee's **relational path**: the graph is built from the
rows without an LLM, and every date cell becomes a `Timestamp` edge, so you can ask how a
domain changed over time.

The Ahrefs API is a paid add-on billed in units. The connector only reads Domain Rating by
default, checks the worst-case cost of a sync before it sends a request, and reports the
units Ahrefs charged.

## Requirements

- cognee 1.6.3 or later, below 2. Tested with 1.6.3 and 1.6.6.
- Python 3.10 to 3.14.
- An Ahrefs plan with API access, or the free test targets `ahrefs.com` and `wordcount.com`.

## Install

```bash
uv pip install cognee-community-connector-ahrefs
# or, from this monorepo:
cd packages/connector/ahrefs && uv sync
```

## Setup

1. In Ahrefs, open **Account settings > API keys** and create a key. Only workspace owners
   and admins can do this.
2. Give the key a **monthly unit limit**. The connector checks the units left before it
   requests anything, but a limit on the key is the only hard cap.
3. For Site Audit, copy each project id from the project URL:
   `https://app.ahrefs.com/site-audit/<project_id>/...`.
4. Set `AHREFS_API_KEY`, and your `LLM_API_KEY` like any other cognee run.

To try it for free, use `wordcount.com` or `ahrefs.com` as the target. Ahrefs does not
charge units for Site Explorer requests about those domains.

## Usage

```python
import cognee
from cognee_community_connector_ahrefs import ahrefs_source

await cognee.remember(
    ahrefs_source(["example.com"], reports=["domain_rating", "backlinks"]),
    dataset_name="seo",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # no row cap; a report exceeds the default 50
    incremental_loading=False,  # required: re-read each report on every sync
    data_cache=False,
)

answer = await cognee.recall("Which sites link to example.com?", datasets=["seo"])
```

Run the same `remember` call again whenever you want to sync. Weekly or monthly is
usually enough; Ahrefs data does not change faster than its crawl. See
`examples/example.py` for the full flow.

Each report keeps one cognee data id across syncs, so `incremental_loading=False` and
`data_cache=False` are needed: without them cognee skips the changed report as already
added. A report whose rows did not change keeps the same content and is not rebuilt.

| Argument | Default | What it does |
|---|---|---|
| `targets` | – | Domains or URLs for the Site Explorer reports |
| `reports` | `["domain_rating"]` | Any of `domain_rating`, `backlinks`, `organic_keywords`, `site_audit` |
| `project_ids` | – | Site Audit project ids, needed for `site_audit` |
| `api_key` | `AHREFS_API_KEY` | Ahrefs API key |
| `max_units` | `1000` | Most units one sync may use, worst case; building the sources fails above it |
| `mode` | `subdomains` | Target scope: `exact`, `prefix`, `domain` or `subdomains` |
| `history_months` | `12` | Domain Rating history read on the first sync |
| `history_grouping` | `monthly` | `daily`, `weekly` or `monthly` Domain Rating points |
| `backlinks_limit` | `50` | Strongest backlinks tracked per target |
| `keywords_limit` | `50` | Best-ranked keywords kept per target and snapshot |
| `country` | all | Two-letter country for keyword rankings |
| `lost_lookback_days` | `90` | How far back lost backlinks are read, and at least back to the last sync |
| `keep_snapshots` | `12` | Keyword and Site Audit snapshots kept per target or project |
| `resource_name` | `ahrefs` | Prefix of the table, source and sync state names: `<resource_name>_<report>` |
| `check_active` | – | Host checkpoint, called before every request and around every row |
| `session` / `service` | – | A `requests` session for the API, mainly for tests |

Give each Ahrefs account or configuration its own `resource_name` when several sync into
one dataset. Each one then keeps its own tables and sync state, so one sync never forgets
the other's targets.

After a run, `cognee_sync_stats` on the source and on its resource holds int counts only:
`requests`, `retries`, `units_estimated`, `units_used`, `rows`, `deleted`, `truncated`,
`failed_auth`, `failed_rate_limit`, and the units left on the key and the workspace.

## What each report stores

| Report | Table | One row per | Fields |
|---|---|---|---|
| `domain_rating` | `ahrefs_domain_rating` | target and date | `domain_rating` |
| `backlinks` | `ahrefs_backlinks` | target, linking page, linked URL and anchor | `domain_rating_source`, `is_dofollow`, `first_seen_link`, `last_visited` |
| `organic_keywords` | `ahrefs_organic_keywords` | target, snapshot date, country and keyword | `position`, `url` |
| `site_audit` | `ahrefs_site_audit` | project, snapshot date and issue | `issue`, `importance`, `category`, `urls_affected`, `change` |

Each report is its own `dlt` source, so it becomes its own cognee data item
(`dlt_source:ahrefs_backlinks`, ...). Syncing one report never rebuilds another.

## Cost

Ahrefs charges `max(50, fields x rows)` units per request. Most fields cost 1 unit; some
cost 5 or 10.

- **Only 1-unit fields are requested.** `traffic`, `volume` and `keyword_difficulty` are
  not read.
- **Worst-case estimate.** Before anything is sent, the connector adds up the most each
  request can cost: the field count times the `limit`, or 50. If the total is above
  `max_units`, building the sources fails with the cost per report.
- **Units left.** Before the first paid request of a report, the connector calls Ahrefs'
  free `limits-and-usage` endpoint. If the key or the workspace has fewer units left than
  the estimate, the sync stops and nothing is requested.
- **Units used.** The `x-api-units-cost-total-actual` header of each response is added to
  `cognee_sync_stats["units_used"]`.
- **Free targets.** Requests about `ahrefs.com` and `wordcount.com` are estimated at 0 and
  skip the usage check.

With the defaults, one target costs at most 50 units for Domain Rating, 400 for backlinks
and 200 for keywords. A Site Audit project costs 50 units.

## How sync and forget-on-delete work

- **Domain Rating** is a dated series. The first sync reads `history_months`. Later syncs
  read from the last stored date, so the open month is refreshed and new months are added.
- **Backlinks** are the strongest `backlinks_limit` links, with the links lost in the last
  `lost_lookback_days`, or since the last sync if that is longer ago. A long pause between
  syncs does not hide a lost link.
  - The list is limited, so a link that is missing from it is **never** forgotten. If 500
    links are stored and the next call returns 100, the other 400 stay as they were last
    seen.
  - A link is forgotten only when Ahrefs marks it as lost.
- **Keyword rankings** and **Site Audit issues** are dated snapshots, one per day.
  - The last `keep_snapshots` snapshots are kept; older ones are forgotten.
  - A second sync on the same day replaces that day's snapshot.
  - Site Audit issues that affect no URL are skipped.
- **Removing** a target or a Site Audit project from the config forgets its rows.
  A Site Audit project that answers 404 is forgotten too.
- Forgotten rows are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete`
  column. dlt removes them on `merge`, cognee rebuilds the report from the remaining rows,
  and the forgotten rows leave the graph and vector stores.
- **Errors never delete.** Every request is made before the first row is emitted, and the
  state is written only after the last row. A failed run changes nothing.
  - 429, 5xx and dropped connections are tried 4 times in total. The wait is `Retry-After`
    when Ahrefs sends it, else 2, 4 and 8 seconds.
  - If Ahrefs asks to wait more than 60 seconds, the run stops with
    `AhrefsRateLimitError` and does not sleep. cognee holds its dlt staging lock during a
    sync, so a long sleep would block other syncs.
  - Other errors, like 400, 401 and 403, raise `AhrefsAPIError` at once. The message has
    the status, the endpoint and Ahrefs' own error text, with the key masked.
  - Each request has a 60-second timeout.
  - Only a confirmed 404 for a whole Site Audit project forgets it.

## Privacy

**What is read**

- Domain Rating history, backlinks and organic keywords of the selected targets, and the
  Site Audit issue list of the selected projects. Only the fields listed above.
- Backlinks are public web pages; the anchor text and URLs are stored as Ahrefs reports
  them.
- The connector only reads. It never changes anything in Ahrefs.

**Where it goes**

- The rows become cognee data. Their text is embedded by your configured embedding
  provider. The relational path does not send rows to an LLM to build the graph.
- dlt state stores the row ids, the last Domain Rating date and the snapshot dates.
- Logs and `cognee_sync_stats` contain counts only. The API key is never in an error or a
  log line; a test checks this for every error status.
- Keep the API key out of code and version control. Use `AHREFS_API_KEY`.

## Limitations

- Backlinks outside the strongest `backlinks_limit` are not tracked.
- If every row of a report is forgotten (for example, every tracked link is lost),
  cognee keeps the last data of that report: its relational path does not delete on an
  empty read. Forget that data item or the dataset with `cognee.forget`.
- To stop syncing a report, forget its data item. Dropping it from `reports` only stops
  the sync.

## Testing

```bash
uv sync --group dev
uv run pytest tests/
```

No Ahrefs key or network is needed. `tests/fake_ahrefs.py` is an in-memory Ahrefs API v3
mounted on a `requests` session, so every request still goes through dlt's `rest_api`
code. It answers with one page, honors `limit`, and sets the unit cost header.

- `test_ahrefs_sync.py` covers the request shape, the Domain Rating date cursor, the
  500-to-100 backlinks case, lost links, the same link twice, a long pause between syncs,
  keyword and Site Audit snapshots with retention and same-day replacement, a deleted
  Site Audit project, a removed target, and errors that never delete. It checks retries
  for 429, 5xx and dropped connections, a long `Retry-After` that stops the run, the
  timeout, `check_active` before every request and retry, the key never showing in an
  error or log, unit headers, the usage check and free targets. Random histories (seeded)
  compare backlinks and snapshots with a simple model over 40 syncs each.
- `test_ahrefs_source.py` covers the factory: one source per report on the relational
  path, argument checks, `resource_name`, the `service` alias, the `max_units` budget, the
  cost formula and free targets. Real dlt runs check that a failed sync keeps the rows,
  that two accounts in one dataset keep their own rows and state, and that 2,000 links over
  6 syncs keep staging equal to the model.
- `test_ahrefs_forget.py` runs the real `cognee.remember` (LLM and embeddings mocked) and
  checks that a lost backlink leaves the graph while the other link and the Domain Rating
  report stay.
