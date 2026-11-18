# Verified-source adaptation

Based on dlt-hub/verified-sources `sources/jira/__init__.py`, revision
`3957506893a7da821dbcc6acd51c7ca4475d1f53`, Apache-2.0. The original license is
included as VERIFIED_SOURCE_LICENSE.

Source: https://github.com/dlt-hub/verified-sources/tree/3957506893a7da821dbcc6acd51c7ca4475d1f53/sources/jira

The adapted `_verified` module preserves the verified source's enhanced-search
resource/page-loop structure and dlt HTTP helper. Changes: injected bounded
transport; failFast replaces the obsolete validateQuery parameter; continuation
survives empty pages; malformed/repeated/conflicting cursors and search warnings
abort; comment offset pagination validates offsets, totals and repeated IDs;
page count is bounded. No projects/users/changelog relational tables are exposed.

The public factory uses the adapted shared paginator to assemble document-mode
snapshots. Additional read routes supply complete comments, attachment inventories,
numeric-ID attachment content, issue detail and authenticated account/timezone.
The transport uses dlt's requests Client with a single bounded retry owner,
allowlisted HTTPS site routes, no redirects, byte limits and sanitized errors.

The verified source's raw nested issue rows and changed-only extraction cannot
alone satisfy Cognee's document cleanup contract. The wrapper retains issue
descriptions and immutable attachment text in source-level dlt state and re-emits
all selected documents under replace after successful validation. Comments and
attachment inventories are refreshed independently every run. This is a material
adaptation, not a claim that the unmodified upstream source has these semantics.

The wrapper reconciles the union of retained IDs and IDs discovered by general
search in batches of at most50, including on the first sync. Each complete
reconciled batch replaces membership for those IDs, so stale search cannot retain
or resurrect an issue that no longer satisfies JQL. Unknown issues absent from
both retained state and discovery remain subject to eventual consistency.
