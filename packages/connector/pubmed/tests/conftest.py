"""Minimal cognee test double for parser/client unit tests.

The connector declares cognee as a runtime dependency.  These tests intentionally
exercise only the NCBI boundary, so they remain runnable without installing the
full graph stack.
"""

import logging
import sys
import types


if "cognee" not in sys.modules:
    cognee = types.ModuleType("cognee")
    shared = types.ModuleType("cognee.shared")
    logging_utils = types.ModuleType("cognee.shared.logging_utils")
    tasks = types.ModuleType("cognee.tasks")
    ingestion = types.ModuleType("cognee.tasks.ingestion")
    dlt_utils = types.ModuleType("cognee.tasks.ingestion.dlt_utils")

    logging_utils.get_logger = logging.getLogger
    dlt_utils.DOCUMENT_SOURCE_ATTR = "cognee_document_source"
    sys.modules.update(
        {
            "cognee": cognee,
            "cognee.shared": shared,
            "cognee.shared.logging_utils": logging_utils,
            "cognee.tasks": tasks,
            "cognee.tasks.ingestion": ingestion,
            "cognee.tasks.ingestion.dlt_utils": dlt_utils,
        }
    )
