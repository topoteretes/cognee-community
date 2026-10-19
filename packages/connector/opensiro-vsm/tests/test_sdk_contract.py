"""Offline Cognee 1.6.4 SDK surface contract used by this connector.

These tests intentionally inspect both documented and internal Cognee APIs:
the latter are version-bound and require revalidation when Cognee upgrades.
"""

import inspect
import unittest
from importlib.metadata import version

import cognee
from cognee.context_global_variables import set_database_global_context_variables
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.provenance import make_source_ref_key
from cognee.modules.data.methods.get_datasets_by_name import get_datasets_by_name
from cognee.modules.search.types import SearchType
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_opensiro_vsm.native import add_data_points, ingest_fresh


class CogneeContractTests(unittest.TestCase):
    def test_compatible_version(self):
        self.assertEqual(version("cognee"), "1.6.4")

    def test_native_write_and_read_surfaces(self):
        self.assertIn("graph_only", inspect.signature(add_data_points).parameters)
        self.assertIn("attempt_root", inspect.signature(ingest_fresh).parameters)
        self.assertIn("query_type", inspect.signature(cognee.search).parameters)
        self.assertIn("datasets", inspect.signature(cognee.search).parameters)
        self.assertTrue(hasattr(SearchType, "CYPHER"))
        self.assertTrue(hasattr(SearchType, "CHUNKS_LEXICAL"))
        self.assertTrue(callable(get_graph_engine))
        self.assertTrue(callable(make_source_ref_key))

    def test_provenance_and_existing_dataset_surfaces(self):
        self.assertIn(
            "permission_type", inspect.signature(set_database_global_context_variables).parameters
        )
        self.assertIn("user_id", inspect.signature(get_datasets_by_name).parameters)
        self.assertIsInstance(DOCUMENT_SOURCE_ATTR, str)
        self.assertTrue(DOCUMENT_SOURCE_ATTR)


if __name__ == "__main__":
    unittest.main()
