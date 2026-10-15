"""DLT source for Snowflake tables and queries (incremental sync + forget-on-delete).

Work in progress. The planned design:

* Key-pair auth with an optional passphrase, plus a connection check that names
  the usual setup mistakes (account identifier, key not registered on the user,
  wrong passphrase, role without access, suspended warehouse).
* Rows from selected tables or user queries go down cognee's relational path.
  Each table also gets a short description document built from its table and
  column comments, so cognee knows what the data means.
* Table and column comments are read with metadata commands, and a table whose
  last altered time has not moved since the last sync is skipped, so an
  unchanged sync does not need to wake the warehouse.
* A user chosen timestamp column is the incremental cursor, with rows that
  share the boundary timestamp tracked so none are skipped.
* Deleted rows come from change tracking where it is enabled, otherwise from
  comparing the key column with the last sync. A failed query never deletes.
"""


def snowflake_source(*args, **kwargs):
    """Create a dlt source that yields Snowflake rows and table descriptions."""
    raise NotImplementedError("The Snowflake connector is still being built.")
