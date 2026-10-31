"""Live validation: real DataHub -> datahub_source -> cognee.remember -> recall.

Skipped unless DATAHUB_LIVE_URL is set. Needs an LLM configured for cognee and
the lab's retail_demo metadata (seed/ in
https://github.com/jars-demo/cognee-data-lab), e.g.:

    DATAHUB_LIVE_URL=http://localhost:8080 DATAHUB_LIVE_TOKEN=... \
    uv run pytest tests -m live -ra -s

Checks run on the context cognee retrieves (only_context=True), not on the
generated wording.
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.live

LIVE_URL = os.environ.get("DATAHUB_LIVE_URL")


@pytest.fixture(scope="module")
def remembered(tmp_path_factory):
    if not LIVE_URL:
        pytest.skip("DATAHUB_LIVE_URL not set")
    root = tmp_path_factory.mktemp("cognee")
    os.environ.setdefault("DATA_ROOT_DIRECTORY", str(root / "data"))
    os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", str(root / "system"))

    import cognee

    from cognee_community_connector_datahub import datahub_source

    source = datahub_source(
        base_url=LIVE_URL, token=os.environ.get("DATAHUB_LIVE_TOKEN"), platforms=["postgres"]
    )
    asyncio.run(cognee.remember(source, dataset_name="datahub_live", max_rows_per_table=0))
    return cognee


def _context(cognee, question: str) -> str:
    result = asyncio.run(
        cognee.search(
            query_text=question,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=["datahub_live"],
            only_context=True,
        )
    )
    return str(result).lower()


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Who owns retail_demo.analytics.daily_revenue?", ["revenue analytics"]),
        ("Which datasets is daily_revenue derived from?", ["orders", "products"]),
        ("Which column of the customers dataset is tagged PII?", ["email"]),
        ("Which domain does daily_revenue belong to?", ["retail demo"]),
    ],
)
def test_recall_answers_from_datahub_memory(remembered, question, expected):
    context = _context(remembered, question)
    for term in expected:
        assert term in context, f"{term!r} missing from retrieved context for {question!r}"
