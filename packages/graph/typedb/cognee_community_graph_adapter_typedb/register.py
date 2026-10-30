"""Register the TypeDB graph adapter and its per-dataset database handler.

Registration is idempotent and runs on package import (this module is
imported by the package ``__init__``), so all of these are equivalent::

    import cognee_community_graph_adapter_typedb
    from cognee_community_graph_adapter_typedb import register; register()
    import cognee_community_graph_adapter_typedb.register  # noqa: F401

Defining ``register()`` here and re-exporting it from the package keeps the
``pkg.register`` attribute bound to the function even when the submodule is
imported explicitly.
"""

from cognee.infrastructure.databases.dataset_database_handler import (
    use_dataset_database_handler,
)
from cognee.infrastructure.databases.graph import use_graph_adapter

from .typedb_adapter import TypeDBAdapter
from .TypeDBDatasetDatabaseHandler import TypeDBDatasetDatabaseHandler


def register() -> None:
    """Register the "typedb" graph provider and its "typedb" dataset handler.

    Select the handler (cognee's backend-access-control mode, on by default)
    with ``GRAPH_DATASET_DATABASE_HANDLER=typedb``.
    """
    use_graph_adapter("typedb", TypeDBAdapter)
    use_dataset_database_handler("typedb", TypeDBDatasetDatabaseHandler, "typedb")


register()
