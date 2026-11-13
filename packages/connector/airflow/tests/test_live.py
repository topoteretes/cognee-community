"""Live validation: real Airflow -> airflow_source -> cognee.remember -> recall.

Skipped unless AIRFLOW_LIVE_URL is set. Needs an LLM configured for cognee
(LLM_API_KEY, plus LLM_MODEL/LLM_PROVIDER for non-OpenAI models) and the lab's
demo DAGs (infra/ in
https://github.com/jars-demo/cognee-data-lab), e.g.:

    AIRFLOW_LIVE_URL=http://localhost:8088 AIRFLOW_LIVE_USERNAME=admin \
    AIRFLOW_LIVE_PASSWORD=admin uv run pytest tests -m live -ra -s

Checks run on the context cognee retrieves (only_context=True), not on the
generated wording, so they test what memory holds.
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.live

LIVE_URL = os.environ.get("AIRFLOW_LIVE_URL")


@pytest.fixture(scope="module")
def remembered(tmp_path_factory):
    if not LIVE_URL:
        pytest.skip("AIRFLOW_LIVE_URL not set")
    root = tmp_path_factory.mktemp("cognee")
    os.environ.setdefault("DATA_ROOT_DIRECTORY", str(root / "data"))
    os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", str(root / "system"))

    import cognee

    from cognee_community_connector_airflow import airflow_source

    source = airflow_source(
        base_url=LIVE_URL,
        token=os.environ.get("AIRFLOW_LIVE_TOKEN"),
        username=os.environ.get("AIRFLOW_LIVE_USERNAME"),
        password=os.environ.get("AIRFLOW_LIVE_PASSWORD"),
        include_runs=True,
    )
    asyncio.run(cognee.remember(source, dataset_name="airflow_live", max_rows_per_table=0))
    return cognee


def _context(cognee, question: str) -> str:
    result = asyncio.run(
        cognee.search(
            query_text=question,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=["airflow_live"],
            only_context=True,
        )
    )
    return str(result).lower()


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What runs before transform_revenue?", ["validate_orders"]),
        ("Who owns the retail_analytics_pipeline DAG?", ["revenue-analytics"]),
        ("Which task writes retail_demo.analytics.daily_revenue?", ["transform_revenue"]),
        ("Which task failed in run lab_failure_1?", ["validate_orders"]),
    ],
)
def test_recall_answers_from_airflow_memory(remembered, question, expected):
    context = _context(remembered, question)
    for term in expected:
        assert term in context, f"{term!r} missing from retrieved context for {question!r}"
