"""DLT source for Databricks notebooks and Unity Catalog tables (incremental + forget-on-delete).

Work in progress. The planned design:

* Notebooks from the selected workspace folders are exported as source. Markdown
  cells stay prose and code cells stay code. Cell outputs are left out because
  they hold real data, and hardcoded tokens or passwords are scrubbed.
* Each Unity Catalog table in the selected catalogs and schemas becomes a short
  document: description, columns with types and comments, owner, and the view
  definition for views.
* Notebooks and tables are joined through lineage, so a notebook document names
  the tables it reads and writes and a table document names the notebooks that
  use it. Table names found in notebook text are the fallback.
* Query results are off by default. Only queries the user names are run, on a
  warehouse the user names, with a row cap and a timeout.
* Notebook modified times and table updated times drive incremental sync, with
  records tracked by their permanent id so a rename or move is not a delete.
  A full id listing finds deletions, and a failed listing never deletes.
"""


def databricks_source(*args, **kwargs):
    """Create a dlt source that yields Databricks notebooks and tables as documents."""
    raise NotImplementedError("The Databricks connector is still being built.")
