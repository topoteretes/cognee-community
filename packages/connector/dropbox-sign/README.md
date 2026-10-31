# cognee-community-connector-dropbox-sign

A Dropbox Sign (formerly HelloSign) data-source connector for
[cognee](https://github.com/topoteretes/cognee): turn your signature requests into memory, so
an agent can answer "has the vendor signed the renewal yet?", "who still has to sign the
NDA?" or "when did we send the contractor agreement?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every signature request becomes
one document, with its status and each signer's own status, that flows through cognee's
normal cognify entity extraction. What signers typed into the form is never read.

## Install

```bash
uv pip install cognee-community-connector-dropbox-sign
# or, from this monorepo:
cd packages/connector/dropbox-sign && uv sync
```

## Setup

1. Sign in at [app.hellosign.com](https://app.hellosign.com). The free plan includes API test
   mode.
2. Open **Settings**, then **API**, and reveal or generate an API key.
3. Pass it as `api_key=`. The example reads it from `DROPBOX_SIGN_API_KEY`, together with your
   `LLM_API_KEY` like any other cognee run:

```bash
export DROPBOX_SIGN_API_KEY="..."
```

## Usage

```python
import os

import cognee
from cognee_community_connector_dropbox_sign import dropbox_sign_source

await cognee.remember(
    dropbox_sign_source(api_key=os.environ["DROPBOX_SIGN_API_KEY"]),
    dataset_name="dropbox_sign",
    primary_key="id",
    write_disposition="merge",  # required: the add pipeline defaults to "replace"
    max_rows_per_table=0,
)

print(await cognee.recall("Who still has to sign the vendor renewal?"))
```

Run it again whenever you like: only requests whose document changed are sent to cognee, and
canceled requests are removed from memory.

### Options

| Argument | Default | What it does |
| --- | --- | --- |
| `api_key` | required | A Dropbox Sign API key. Never read from the environment by the source itself. |
| `account_id` | the key's account | A team member's account id, or `"all"` for the whole team. |
| `query` | none | A Dropbox Sign search query that narrows the listing, for example `"title:NDA"`. |
| `since_days` | `365` | Only requests created within this many days. |
| `include_test_mode` | `False` | Also sync test-mode requests, which have no legal value. |
| `include_emails` | `False` | Put signer and requester email addresses in the documents. |
| `full_resync` | `False` | Emit every request again. |
| `max_requests` | `1000` | Request budget of one run. |
| `resource_name` | derived | Staging table name, which scopes this sync's state and cleanup. |
| `check_active` | none | Host authorization checkpoint, called before every request and during retry waits. |

## What a document looks like

```text
Signature request: Vendor renewal
Subject: Please sign: Vendor renewal
Status: awaiting signatures
Sent: 2026-10-30 09:12 UTC
Expires: 2026-11-30 09:12 UTC

Signers:
- Priya Shah (Client): signed on 2026-10-30 11:40 UTC
- Sam Lee (Vendor): awaiting signature, last viewed 2026-10-31 08:02 UTC

Renewal of the 2027 support contract.
```

The status is `complete`, `declined`, `error`, `expired` or `awaiting signatures`. A signer's
decline or reassignment reason is included. The document links to the request's details page.

## What is never read

Documents are built from an allowlist of fields, so these never reach memory:

- `response_data`: the values signers typed into form fields (bank details, addresses, IDs).
- `custom_fields` and `metadata`.
- Attachments, the files URL, final copy URI and signing URLs.
- Signers' SMS phone numbers and CC addresses.
- Email addresses, unless `include_emails=True`. Signers are named by name and role, or as
  "Signer 1", "Signer 2".

Every title and content then goes through a scrubbing step (`redact.scrub`, the same as the
Terraform Cloud connector's) that redacts private keys, cloud and VCS tokens, JWTs, URL
credentials and secret-looking `key = value` pairs in messages and reasons. Redaction counts
land in `cognee_sync_stats["redacted"]`.

## How the sync works

- **One listing per run.** `GET /account` names the account, then
  `GET /signature_request/list` is read 100 requests a page. The listing carries each request
  whole, signers included, so no other request is needed.
- **Changed requests only.** Each request's rendered document is hashed; a request is emitted
  again only when that hash changes, for example when a signer views, signs or declines it.
- **Every page is read.** Dropbox Sign does not document the listing's order, so the sync
  never stops early on it. Requests created before `since_days` are skipped; one that ages out
  of the window stays in memory as last synced.
- **Forget-on-delete.** A request missing from a complete listing (canceled, removed, or
  outside a new `query`) is removed from memory. A listing that comes back empty while
  requests were synced before forgets nothing until the next run confirms it, and a listing
  without its `signature_requests` list, or with a request it cannot name, raises instead of
  forgetting anything.
- **One account per state.** The state remembers the account it was made with; a different
  key under the same `resource_name` raises instead of forgetting the first account's
  requests. Give each account its own `resource_name`.
- **A run is bounded.** 429 waits until `X-Ratelimit-Reset` (Dropbox Sign sends no
  `Retry-After`), and 5xx is retried. A reset more than 60 seconds away, a 429 that outlasts
  the retries, or more than five minutes of retry waits stops the run cleanly with `failed`
  and `failed_rate_limit`; a spent `max_requests` budget stops it with `failed` and
  `failed_budget`. A key rejected after the first page stops it with `failed` and
  `failed_auth`; a key rejected at the start raises. A run cut short forgets nothing. Any other
  error raises, so dlt keeps the previous state.

`cognee_sync_stats` always carries `scanned`, `emitted`, `unchanged`, `deleted`,
`skipped_test_mode`, `redacted`, `failed`, `failed_auth`, `failed_budget` and
`failed_rate_limit`.

## Privacy

Signature requests name people and the agreements they sign. Nothing is fetched until you
construct a source and call `remember`, and anyone with read access to the target dataset can
read what was ingested. Use a dataset with the right permissions, and keep `include_emails`
off unless you need it.

## Development

```bash
cd packages/connector/dropbox-sign
uv sync
uv run pytest tests/
```

The tests need no account or LLM key.
