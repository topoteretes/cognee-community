# Unreleased — Postman connector

- **Breaking change:** callers must pass a stable `resource_name=`; omitted names raise
  TypeError. Distinct names keep independent connections separate while selection stays mutable.
- Source construction performs no provider I/O. Account verification occurs during counted
  extraction; durable state rejects account changes. Names use workspace/owner/dataset/resource
  inputs, so minimal and explicit-account calls share the same namespace.
- **Migration:** previous experimental selection-based, default-name and account-named
  identities can leave old tables/documents behind. Retire the old owned scope before
  upgrading or migrate via a dedicated dataset; do not broadly clear shared datasets.
- Empty provider inventories over populated caches and explicitly selected missing collections
  require confirmation. Automatic all-visible removals use a configurable rolling five-minute
  budget (one by default); refusal diagnostics list unconfirmed UIDs.
- Endpoint templates are enabled by default; empty metadata labels are omitted. Request UI
  provenance requires a provider-supplied request UID, otherwise the collection link is used.
- Renderer policy6 narrows query-token heuristics, preserves short prose values, filters
  sensitive JSON body fields/custom headers and additional known token paths. Filtering
  remains best effort; values below four characters skip global prose replacement.
- Three-read unchanged sync, centralized authorization checkpoints, all-exception failure
  counts with exception-class diagnostics, malformed-item rejection and Python>=3.10,<3.15.
- Public example uses remember. Blog and credential-writing setup removed; isolated
  validation helpers remain test support. Committed uv.lock and exact receipts accompany validation.
