# cognee-community-connector-dropbox-sign

A Dropbox Sign (formerly HelloSign) data-source connector for
[cognee](https://github.com/topoteretes/cognee): turn your signature requests into memory, so
an agent can answer "has the vendor signed the renewal yet?" or "who still has to sign the
NDA?".

**Status: work in progress.** This draft sets out the package and the design; the
implementation and tests follow in this PR.

## Planned design

- **Source:** the Dropbox Sign API v3 (`https://api.hellosign.com/v3`), authenticated with an
  API key (HTTP basic auth, key as the user name). Read-only: `GET /signature_request/list`.
- **Documents:** one per signature request (`signature_request:<id>`): title, subject, status
  (awaiting signature, complete, declined, canceled), who sent it, when, and each signer with
  their own status and dates. The request's message is included, scrubbed.
- **Never read:** form field values (`response_data`), custom field values, file URLs,
  signing URLs and attachments. Documents are built from an allowlist of fields.
- **Incremental:** requests are listed newest first within `since_days`; a request is emitted
  again only when its rendered document changes (a signer signs, declines or views it).
- **Forget-on-delete:** a request that leaves a complete listing (canceled or removed) is
  emitted as a hard-delete tombstone; an empty listing forgets nothing until the next run
  confirms it.
- **Bounded runs:** 429 and 5xx are retried with `Retry-After`; a long rate limit or a spent
  request budget stops the run cleanly, reported in `cognee_sync_stats`.
- **Test mode:** test-mode requests are left out unless `include_test_mode=True`.

Closes topoteretes/cognee#6142.
