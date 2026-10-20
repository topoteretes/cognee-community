"""DLT source for Snowflake tables (incremental sync + forget-on-delete).

Work in progress. The design, based on tests against a real Snowflake account:

* Key-pair auth with an optional passphrase, plus a connection check that names
  the usual setup mistakes. Key-pair login has to use the user's login name,
  which on a trial account is the email address, not the user name shown in
  Snowsight; with the user name, or with a key that is not registered, Snowflake
  answers only "JWT token is invalid".
* Table and column comments come from SHOW TABLES and SHOW COLUMNS, which run
  without a warehouse. Each table becomes a short description document, so
  cognee knows what the data means.
* Whether a table changed at all comes from SYSTEM$LAST_CHANGE_COMMIT_TIME,
  which also runs without a warehouse and moves on every insert, update and
  delete. An unchanged table is skipped without waking the warehouse. SHOW
  TABLES has no last altered time, and INFORMATION_SCHEMA does need a warehouse.
* Rows of a changed table go down cognee's relational path. With change
  tracking enabled and inside the retention window, only the changes are read
  with CHANGES(INFORMATION => DEFAULT), deletes included. Otherwise a timestamp
  column is the cursor (read with >=, so rows sharing the boundary timestamp
  are never skipped) and deletes come from comparing the primary keys with the
  last sync. A failed query never deletes.

Rows are read with Snowflake's own connector inside a dlt resource, like the
Google Drive and Google Calendar connectors wrap Google's client. dlt's
sql_database source logs in with a key pair too, but it always queries (so it
wakes the warehouse on every sync), never sees deletes, cannot express CHANGES,
and does not map Snowflake's TIMESTAMP_NTZ type.
"""


def snowflake_source(*args, **kwargs):
    """Create a dlt source that yields Snowflake rows and table descriptions."""
    raise NotImplementedError("The Snowflake connector is still being built.")
