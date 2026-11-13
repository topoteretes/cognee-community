# cognee-community-connector-docusign

A DocuSign data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your
signed agreements into memory, so you can ask "which NDAs are we under with this vendor?" or "when
was this contractor's agreement signed, and by whom?".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Every envelope is
rendered to text and ingested as a **normal document**: a short header (subject, sender, each
signer with their role and signing date, custom fields, what signers typed into form fields)
followed by the text of the signed PDF. It goes through cognee's cognify entity-extraction
pipeline, so signers and companies become graph nodes linked to the agreement. Re-syncs are
**incremental**, and envelopes that are voided, moved to the Deleted folder or purged are
**forgotten** on the next sync.

## What gets stored

Contracts are confidential, so to be explicit. For every envelope in scope the connector stores,
in your cognee databases, and sends to your LLM provider during `cognify`:

- the text layer of the combined signed PDF (all documents in the envelope),
- the subject, sender name and email, sent and completed dates,
- each recipient's name, email, role, status and signing date,
- the envelope's custom fields, and values signers typed into form fields.

It does **not** store the PDF file itself, and the certificate of completion (signer IP addresses,
audit trail) is left out unless you pass `include_certificates=True`. Scanned PDFs have no text
layer; the header is ingested and the envelope is counted in `pdf_no_text` (see
[Sync stats](#sync-stats)). Use a dedicated dataset so you can remove everything with one
`cognee.forget`.

## Requirements

Pinned to cognee 1.6.4. The connector relies on document-mode (`DOCUMENT_SOURCE_ATTR`),
table-scoped document cleanup, per-dataset pipeline state (`PIPELINE_SCOPE_ATTR`) and per-row node
sets (`cognee_node_set`). `docusign_source` raises on a cognee that lacks them.

## Install

```bash
uv pip install cognee-community-connector-docusign
# or, from this monorepo:
cd packages/connector/docusign && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_docusign import docusign_source

await cognee.remember(
    docusign_source(),  # DOCUSIGN_* credentials from the environment
    dataset_name="docusign",
    write_disposition="merge",  # incremental: later runs only fetch what changed
)

answer = await cognee.search(
    query_text="Which NDAs are we under with Acme?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["docusign"],
)
```

> **Pass `write_disposition="merge"` to sync incrementally.** The add pipeline defaults to
> `"replace"`. Under `"replace"`, dlt resets the connector's state every run, so each run lists
> every envelope in full and loads a complete snapshot. That is still correct (removed envelopes
> drop out and are forgotten), but it re-reads everything.

Choose what to ingest:

| Argument | Effect |
| --- | --- |
| `statuses=["completed"]` | Envelope statuses to ingest. `voided` and `deleted` are rejected: those envelopes are always forgotten. |
| `folder_ids=[...]` | Only envelopes in these folders: a folder id, a folder name (resolved through `GET /folders`), or a system folder such as `sentitems`. An unknown folder raises. Each configured folder also becomes a node set. |
| `from_date="2025-01-01"` | Ignore envelopes whose status changed before this date. |
| `node_set_field="agreement_type"` | Use this envelope custom field's value as the envelope's node set, so `recall` can be scoped to just NDAs. |
| `include_certificates=False` | Append the certificate of completion to each PDF. Off by default (signer IPs, audit data). |
| `include_form_data=True` | Add what signers typed into form fields to the text. One extra request per ingested envelope. |
| `max_requests=None` | Stop cleanly after about this many API requests in one run and resume next run. See [Staying under rate limits](#staying-under-rate-limits). |
| `resource_name="docusign_envelopes"` | Name of the staging table and the incremental state. Every DocuSign source syncing into one dataset needs its own name. |
| `check_active=None` | Optional callable for hosts, run before every request and around each row, to stop a sync whose connection was revoked. |
| `account_id`, `base_uri` | Read a specific account / host. Normally discovered from `/oauth/userinfo`. |
| `environment="demo"` | `"demo"` (developer sandbox) or `"production"`. |

Changing any of these settings makes the next run a full re-list, and envelopes that fell out of
scope are forgotten. See `examples/example.py` for the full flow.

## Authentication

Pass the arguments, or set the environment variables:

| Variable | Meaning |
| --- | --- |
| `DOCUSIGN_INTEGRATION_KEY` | Integration key (client id) of your app |
| `DOCUSIGN_USER_ID` | GUID of the API user the integration acts as |
| `DOCUSIGN_PRIVATE_KEY_PATH` | Path to the RSA private key (PEM). `DOCUSIGN_PRIVATE_KEY` takes the PEM text itself |
| `DOCUSIGN_ACCESS_TOKEN` | A ready access token. Replaces the three above for quick tests |
| `DOCUSIGN_ACCOUNT_ID` | Account to read (default: the user's default account) |
| `DOCUSIGN_BASE_URI` | API host, e.g. `https://na3.docusign.net` (default: from `/oauth/userinfo`) |
| `DOCUSIGN_ENVIRONMENT` | `demo` (default) or `production` |

- **JWT grant** is the way for unattended syncs. The connector signs a one-hour assertion, renews
  the token before it expires, and renews it once if DocuSign answers `401`.
- **An access token** is used as is and cannot be renewed: a sync that outlives it stops with an
  error. This is also how to use the authorization-code flow (the token a user obtains by logging
  in). The token is a user token, so the connector reads what that user can see.
- The API host is never hardcoded. Production accounts live on different hosts (`na2`, `na3`,
  `eu`, ...), so it comes from `/oauth/userinfo` unless you pass `base_uri`.
- Private keys and tokens are never logged or put into error messages.

### Read-only access

The connector only sends `GET` requests. DocuSign has no read-only scope though: the `signature`
scope that the API requires also allows sending and voiding. To keep the blast radius small,
create a **dedicated API user** with a limited permission profile (no sending, no admin rights),
share only the envelopes you want ingested with that user, and use that user's GUID. What the
connector sees depends on that user's access.

## Setup: developer account and integration key

A free developer account is enough to build and test this. Menu labels in DocuSign change now and
then, so treat the wording below as a guide.

1. Sign up at [developers.docusign.com](https://developers.docusign.com). You get a demo account
   at `account-d.docusign.com`.
2. Open the admin console, then **Settings → Integrations → Apps and Keys → Add App and
   Integration Key**. Name it, and copy the **Integration Key**.
3. On the same page, under **My Account Information**, copy the **API Account ID** and the
   **User ID** (the GUID of the user you are logged in as).
4. Under the app's **Authentication**, choose **Authorization Code Grant**, then under **Service
   Integration** click **Generate RSA**. Copy the private key right away and save it as
   `private.pem`: DocuSign shows it only once. Keep it out of version control.
5. Under **Additional settings → Redirect URIs**, add one (for example `https://www.docusign.com`).
   It is only used for the consent step below.
6. Grant consent once, by opening this URL in a browser while logged in as the API user, and
   clicking **Accept**:

   ```text
   https://account-d.docusign.com/oauth/auth?response_type=code&scope=signature%20impersonation&client_id=<integration key>&redirect_uri=<your redirect URI>
   ```

   Without consent the first sync fails with `consent_required`, and the error message points back
   at this step.
7. Export the credentials, plus your `LLM_API_KEY` as for any other cognee run:

   ```bash
   export DOCUSIGN_INTEGRATION_KEY="<integration key>"
   export DOCUSIGN_USER_ID="<user id>"
   export DOCUSIGN_PRIVATE_KEY_PATH="private.pem"
   ```

8. Create and complete a few test envelopes (the demo account lets you send to yourself), then run
   `uv run python examples/example.py`.

### Going to production

Integration keys created in the demo environment must pass DocuSign's **Go-Live** review before
they work against a production account. Once promoted, use the production key, user GUID and a
key pair from your production account, and set `environment="production"` (or
`DOCUSIGN_ENVIRONMENT=production`). Consent is granted separately on `account.docusign.com`.

## How a sync works

Each run lists the envelopes whose **status changed** since the previous run
(`GET /envelopes?from_date=<cursor>&from_to_status=changed`). One listing page already carries the
recipients, custom fields and folders, so an unchanged envelope costs no extra request. For each
envelope that is new or changed, the connector downloads `documents/combined` (and, unless
disabled, `form_data`) and renders one document.

- **Cursor**: the newest `statusChangedDateTime` seen, minus a 5 minute overlap. It only moves when
  the whole run loaded.
- **Unchanged envelopes** that the overlap window re-lists are skipped without a download. cognee
  would also keep them as they are, because the rendered text is identical.
- **Failure safety**: a failed page, a `401`, a response that is not a JSON object, exhausted `5xx`
  retries or a client error abort the run before anything is deleted or the cursor moves. The next
  run retries from the same point. A `429` is waited out when DocuSign asks for a short wait
  (`Retry-After` / `X-RateLimit-Reset`, at most 60 s) and otherwise stops the run cleanly, see
  below.
- **Listing order**: oldest status change first, so a run that stops early can resume at its
  cursor. A listing that keeps answering with the same `nextUri` raises instead of looping.
- **Folders**: DocuSign's list endpoint rejects a custom folder *name* (HTTP 400) and returns
  nothing when a folder filter is combined with several `status` values. So names are turned into
  folder ids first, and with `folder_ids` the status is checked in the connector, not in the query.

### Forget-on-delete

DocuSign envelopes are not really deleted, and the usual "removed" cases do not change the status
date, so a `from_date` listing alone never sees them:

- A **completed** envelope cannot be voided. Removing it means moving it to the **Deleted** folder
  (`recyclebin`) or purging it. Neither changes `statusChangedDateTime`.
- A **purge** only changes `purgeState` (for example `documents_queued`).
- DocuSign **empties the Deleted folder after a while**. The envelope then sits in no folder and
  its documents are no longer readable by the API user (HTTP 400
  `USER_NOT_ENVELOPE_SENDER_OR_RECIPIENT`). A new sync that lists such an envelope skips it
  instead of failing, and an ingested one is forgotten.

So every run does two checks:

1. Envelopes the listing returns that are `voided`, `deleted`, in the Deleted folder or in no
   folder, purged (any `purgeState` but `unpurged`, a queued purge included), unreadable, or no
   longer in scope (status or folder) become tombstones.
2. Every envelope id the connector ingested earlier but the listing did not return is looked up
   again by id (`envelope_ids=...` with folders and purge information, 50 ids per request). Ones
   that are gone, binned, purged or out of scope become tombstones.

A tombstone sets the `_deleted` hard-delete column, so dlt removes the row on `merge` and cognee's
`orphan_cleanup` removes the document's graph and vector nodes.

Safety valves keep a bad response from emptying a dataset: if a full listing returns no envelopes
at all while some were known, nothing is forgotten that run; if the by-id lookup returns nothing
while the listing also returned nothing, nothing is forgotten either; and a run that was cut short
never forgets anything, because its listing is incomplete. A queued purge counts as removed, so if
one is later cancelled, the envelope comes back on the next full re-list (change a setting, or run
with `write_disposition="replace"`).

### Staying under rate limits

DocuSign enforces hourly API limits, so the first sync of a large account may not fit in one hour.
Pass `max_requests=N` and a run stops cleanly after about `N` requests: it keeps what it read,
leaves the cursor at the first envelope it did not reach, and the next run resumes there. Each run
reads at least one envelope, so even a tiny budget converges. A `429` that asks for a long wait
does the same. Such a run reports `failed`, plus `failed_budget` or `failed_rate_limit`, in the
[sync stats](#sync-stats). A scope change over an already ingested dataset needs its whole listing,
so it cannot resume: a long `429` there stops the run with an error and nothing changes.

### Request cost

Per run: one listing request per 100 changed envelopes, one lookup request per 50 *tracked*
envelopes the listing did not return, and one or two requests (PDF, form data) per envelope that is
new or changed. The lookup grows with the number of ingested envelopes, so a large account makes
every run cost a few dozen requests even when nothing changed. DocuSign enforces hourly API limits
and asks integrations not to poll aggressively: run the sync on a schedule (for example every 15
minutes or less often), not in a tight loop. Connect webhooks would make this event-driven; that is
a possible follow-up and is not part of this connector.

### Sync stats

The source exposes counts only, never subjects or contract text, on `source.cognee_sync_stats`:
`full_sync`, `listed`, `ingested`, `unchanged`, `forgotten`, `rechecked`, `pdf_no_text`
(scans), `pdf_unreadable`, `failed`, `failed_rate_limit`, `failed_budget`.

### Limits

- The text comes from the PDF's text layer. Scanned documents have no OCR here: they keep their
  header only.
- A change that DocuSign does not record as a status change (for example editing a custom field on
  a finished envelope) is picked up only if the envelope is re-listed for another reason.
- One `resource_name` per source in a dataset. A run refuses to continue when the stored state
  belongs to a different account, rather than forgetting the other account's envelopes. Renaming a
  source starts it over; documents synced under the old name are not cleaned up.

## Tests

```bash
uv sync
uv run pytest
```

The tests need no DocuSign account. `tests/fake_docusign.py` is mounted on a real `requests`
session, so the JWT exchange, account discovery, `nextUri` paging, retries and token renewal all
run unchanged, and it mimics DocuSign's behaviour that matters here (the Deleted folder and purges
do not move `statusChangedDateTime`, an emptied Deleted folder leaves no folders and unreadable
documents, and a folder filter combined with several statuses returns nothing). `tests/test_docusign_cognee.py` runs the real
`cognee.add` + `cognify` with a mocked LLM and checks that removed envelopes leave the graph and
that an unchanged re-sync makes no LLM call.
