"""DLT source for monday.com boards (incremental sync + forget-on-delete).

Work in progress. The planned design, based on tests against a real account:

* Each item becomes one document on cognee's document path, in the same shape
  as the Linear connector: a state line, an assignee line, a parent line for
  sub-items, the other columns, and the item's updates folded in.
* Every sync lists the ids and last-updated times of all active items, and
  fetches full details only for items that changed.
* Posting or editing an update does not change an item's last-updated time and
  is not in the activity log, so updates are read as their own stream.
* An id missing from a complete listing is looked up by id. Archived or deleted
  items get a ``_deleted`` marker so cognee forgets them. A failed or partial
  listing never deletes anything.
"""


def monday_source(*args, **kwargs):
    """Create a dlt source that yields monday.com items as documents."""
    raise NotImplementedError("The monday.com connector is still being built.")
