"""Keep the test suite fully offline and deterministic.

Importing cognee (for dlt_utils.document_source_tag / DOCUMENT_SOURCE_ATTR)
pulls in its telemetry client, which otherwise fires a real HTTP POST to
cognee's telemetry endpoint on nearly every call made during these tests.
None of that belongs in an offline unit-test run: it adds network flakiness
cognee's tests do not need, and nothing here should depend on it succeeding
or failing.
"""

import os

os.environ.setdefault("TELEMETRY_DISABLED", "1")
os.environ.setdefault("ENV", "test")
