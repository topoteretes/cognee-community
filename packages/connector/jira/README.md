# Jira Cloud connector

One lazy `jira_source` dlt factory for issue, comment and attachment prose in
selected Jira Cloud projects. API-token first pass; no OAuth/Confluence app setup.

## Install and configure

```sh
pip install cognee-community-connector-jira
```

Use an HTTPS site root (`https://your-site.atlassian.net`), your Atlassian email,
and an **unscoped** API token. Scoped tokens use Atlassian's gateway/cloudId and
are unsupported here. The account needs Browse Projects and access to selected
issues, restricted comments and attachments. The connector issues GET only.
Store credentials in a secret manager/private local file; never commit them.

```python
import cognee
from cognee_community_connector_jira import jira_source

source = jira_source(
    site_url=site_url,
    email=email,
    api_token=api_token,
    projects=["ENG"],
    resource_name="engineering",
    missing_policy="mirror_visibility",  # Automatic removal from the visible corpus.
    # Optional JQL filter, combined with selected projects; no ORDER BY.
    jql='labels = "knowledge"',
)
await cognee.remember(
    source,
    dataset_name="jira_docs",
    run_in_background=False,
    dlt_config={"write_disposition": "replace", "primary_key": "id"},
    max_rows_per_table=0,
)
```

`resource_name` names one persistent corpus. Keep it stable while changing project
selection or JQL; selection changes intentionally replace that corpus. Use distinct
names for independent selections, even on the same site. Site, normalized email
and resource name define the source/table/pipeline namespace; token rotation does
not. The first authenticated account is bound in durable state and later mismatch
aborts. Changing email/site creates a new corpus: retire the old corpus explicitly
before abandoning it. Core scopes staging by dataset. Concurrent runs of the same
corpus are unsupported.

Run `examples/quickstart.py` with JIRA_SITE_URL, JIRA_EMAIL, JIRA_API_TOKEN and
comma-separated JIRA_PROJECTS in the environment. Also set the required
JIRA_MISSING_POLICY: use `mirror_visibility` for automatic next-sync removal,
or `require_confirmation` for guarded manual acknowledgment. For example:

```sh
export JIRA_MISSING_POLICY=mirror_visibility
python examples/quickstart.py
```

Configure Cognee's stores/models
normally before running; this example does not redirect existing databases.

## Synchronization and removal contract

Initial sync fetches full issue descriptions. Later runs use a JQL updated window
with five-minute overlap, minute-rounded literals in the account timezone, plus a
full compact inventory. New/timestamp-discrepant issues fall back to direct reads.
Unchanged issue descriptions and attachment text are reused; the entire selected
corpus is still emitted into **replace**, including on unchanged runs. Comments
and attachment inventories refresh independently of parent timestamps. Cheap issue
context (including labels/project names) also refreshes. `force_refresh=True`
invalidates description/attachment reuse. This reduces expensive content fetching;
it is not a delta-only sync, and all comments are read every run.

Numeric issue IDs survive key changes/moves within selected projects. Comment and
attachment documents carry parent titles, author/file context and source links.
These are prose documents, not deterministic relational project/issue graph edges.

The removal policy is a **required explicit choice**, with no silent default.
The setup above uses `missing_policy="mirror_visibility"`: it removes missing
issues, comments and attachments on the next successful foreground sync, including
the final document, **assuming access remains complete and stable**. This is the
automatic forget-on-delete workflow. Lost permissions, project moves and filter
exits can look like deletions; mirroring removes these from the visible corpus too.
Jira search is eventually consistent. Reconciliation of retained and newly discovered IDs (maximum50 per query)
replaces stale general-search membership for those IDs, including filter exits,
but is neither a delete feed nor proof of completeness. Every inventory walk,
including the first sync, incurs these batched reconciliation requests.

Choose `missing_policy="require_confirmation"` instead when ambiguity should block
removal. This optional guarded mode does **not** provide automatic next-sync
forgetting. A missing previous issue/child aborts and lists stable document IDs
(for example `issue:10001:comment:10002`). Verify access, then pass those exact IDs
in `confirmed_removed=[...]`. Acknowledgment cannot remove a visible document;
issue deletion requires acknowledging prior child IDs too. There is no expiring
automatic-removal budget. The guard/account binding needs retained source state;
resetting staging loses that history and cannot reconstruct it from Jira.

An empty successful snapshot explicitly materializes its table, even when staging
has been rebuilt, so Cognee receives the correct cleanup scope. Rebuilt staging
with visibility mirroring can retire retained memory. Rebuild/selection changes
are deliberate corpus replacement, not evidence that Jira issued a delete event.
No failed extraction is published as an authoritative partial snapshot.

All pages/required reads/rendering must succeed before any row is emitted.
Repeated inventories and child reads detect common races, not an atomic snapshot.
Previously unknown issues can appear late despite overlap. Retained staging lets
an unchanged rerun retry downstream ingestion. If dlt has a pending load package,
its next run may only replay that package; run a fresh source afterward. The
example does this when extraction counters show no fresh extraction occurred.
Extraction statistics do not certify graph/vector cleanup. Core can log cleanup
errors without raising; inspect stores after important removal operations.

## Bounds, privacy and compatibility

Defaults: 1,000 issues, 200 pages per walk, 50 results per page, 2 MiB per attachment,
20,000 documents, 32 MiB serialized rows+state. Each HTTP response is bounded to
8 MiB for JSON and 32 MiB accumulated issues/comments per walk; four attempts
with delay capped at 60 seconds and bounded read intervals. The stream time check is cooperative, not a hard OS deadline. Work and
temporary allocations can exceed the final serialized-state budget. Large scopes
and same-scope concurrent runs are not supported.

UTF-8 text/plain, markdown, CSV, JSON, XML and text PDFs are supported. PDF workers
have a 10-second deadline, 100-page and 200,000-character limits; there is no hard
RSS cap. No OCR or Office extraction. Unsupported/oversized attachments produce
explicit marker documents. Supported read/extraction failures abort the snapshot.
Blank PDFs are marked as having no extractable text. Attachment URLs returned by
Jira are never fetched; numeric-ID content routes disable redirects and require
full HTTP 200. The connector does not execute links or use filenames as paths.

Only selected fields are rendered; custom fields, changelogs, worklogs and arbitrary
raw issue payloads are excluded. User-authored descriptions/comments/attachments
may contain secrets: this connector is not a general secret detector. Credentials
are not logged or included in resource identity. Scope access before ingestion.

Targets Cognee >=1.6.5,<1.7 with document-sync contract >=1, dlt >=1.30,<1.31,
Python 3.11–3.13. Actual tested versions and outstanding validation are reported
separately; declared ranges are not proof of every combination. Provenance and
verified-source changes are in [VERIFIED_SOURCE.md](VERIFIED_SOURCE.md).

## Development

```sh
uv sync --extra test
uv run pytest tests
uv run ruff check .
uv run ruff format --check .
```
