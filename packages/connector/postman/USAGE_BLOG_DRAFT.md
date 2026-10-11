# Turning Postman request documentation into Cognee memory

Draft for the contributor to review, edit and understand before publication.

The Postman connector reads selected collections and turns each request into a
searchable document. Collection and folder descriptions provide context without
building a graph that simply reproduces the collection JSON tree. The connector
never executes saved requests.

The demonstration used a dedicated synthetic collection in an owned workspace.
An API key was entered through a hidden terminal prompt and saved outside Git with
private permissions. Account and workspace identity were verified through Postman.
The account reported 10,000 monthly calls; inventory used a separate 10-calls-per-10s
limit. Those are measured account headers, not an allowance promised to every user.

For incremental fetching, the connector compares collection updatedAt values.
Unchanged trees stay cached as sanitized request documents. Cognee receives a complete
replace snapshot on every run: sending only changed rows would erase unrelated
retained documents. Inventory validation happens before any rows are published.

An early offline test revealed that dlt clears resource state for replace loads.
Moving the cache to source state allowed unchanged runs to reuse collection trees.
The cache still participates in dlt's load/recovery behavior and contains no raw
provider trees. A no-change retry can re-emit the complete corpus after later
Cognee ingestion or cleanup failure.

The live fixture said “Nova lives in Cedar City.” Local Cognee ingestion and chunk
search found it. After editing the request to “Nova lives in Aster City,” the new
text was searchable and old text disappeared from Data, chunk vectors and graph
properties. Removing the last request left zero Data records, graph nodes/edges,
and rows in all six LanceDB tables. A subsequent empty sync kept them empty.

Two deliberate failure tests checked recovery. One stopped ingestion after dlt
staging; the retry recovered without a collection tree fetch. Another failed before
graph/vector deletion; the stale records visibly remained, then a no-change retry
removed them after the deletion function was restored. Core warnings alone are
not evidence that cleanup succeeded: inspecting all stores mattered.

Whole-collection disappearance may mean deletion, unsharing or moving. The source
API defaults to requiring confirmation. The explicit mirror_visibility policy, used
by the examples, treats absence from a complete validated workspace listing as removal.
It automatically forgets previously observed collections on the next sync, including
visibility loss. Unknown selected UIDs and failed inventories still abort. The final
clean installed-wheel lifecycle deleted a nonempty collection, verified its removal
while its sibling remained, then deleted the last collection and proved all stores
empty. A separate real-store test preserved another populated source scope in the
same dataset while forgetting the first.

The final runtime was a clean PyPI installation of Cognee v1.6.3, dlt 1.30.0,
SQLite, Ladybug and LanceDB, with
already cached GLiNER2.5 and bge-small local models. No paid model calls or model
downloads were needed. The offline suite passed 60 tests. The final built wheel was installed
in that clean environment. Both runnable examples passed ingestion, unchanged sync
and automatic collection deletion with real store verification. The README contains reproducible commands and tested limitations.

Known limitations include positional fallback IDs for missing/duplicate item IDs,
nontransactional provider snapshots, explicit scope migration and incomplete secret
detection for arbitrary prose. Only reviewed documentation should be ingested.
The live demonstration used small synthetic fixtures; it does not prove every
provider permission scenario, concurrent edit pattern or partial database failure.
