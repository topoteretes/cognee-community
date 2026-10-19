# Unreleased — Postman connector

- Required stable resource_name prevents accidental sharing between independent sources.
- Account identity is resolved before source naming; minimal and explicit-account calls
  use the same namespace when their other ownership inputs match.
- **Migration:** previous experimental identities can leave old tables/documents behind;
  retire the old owned scope before upgrading or migrate via a dedicated dataset.
- Default visibility mirroring has a configurable bulk-removal guard (one unconfirmed
  missing collection allowed; larger removals require explicit confirmation by default).
- Endpoint templates enabled by default, request UI provenance, renderer policy5 and
  regressions for secret variables/auth headers/query/path tokens and short-secret bounds.
- Three-read unchanged sync, centralized authorization checkpoints, all-exception failure
  counts, malformed-item rejection and Python>=3.10,<3.15 compatibility declaration.
- Public example uses remember. Blog and credential-writing setup removed; isolated
  validation helpers are explicitly test support. Exact receipts accompany validation.
