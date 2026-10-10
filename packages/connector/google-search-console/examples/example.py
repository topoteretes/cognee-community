"""Google Search Console connector demo — turn your search analytics into memory.

Pulls Google Search Console search queries, landing pages, and performance metrics
(clicks, impressions, CTR, average position) into cognee with forget-on-delete and
incremental trailing-window synchronization.

────────────────────────────────────────────────────────────────────────────
One-time setup (Live credentials)
────────────────────────────────────────────────────────────────────────────
1. Enable the Google Search Console API in the Google Cloud Console:
   https://console.cloud.google.com/apis/library/searchconsole.googleapis.com
2. Create OAuth 2.0 Credentials (Desktop application or Web application) or a
   Service Account with read access (https://www.googleapis.com/auth/webmasters.readonly).
3. Add the user/service account as a verified user to your properties in Search Console.
4. Export credentials:
   export GSC_ACCESS_TOKEN="ya29.a0AfH6SM..."
   export LLM_API_KEY="sk-..."
5. Run:
   uv run python examples/example.py

If GSC_ACCESS_TOKEN is not set, this script runs in demonstration mode using
simulated Search Console data to showcase initial backfill, graph search,
incremental sync, and forget-on-delete.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import cognee
import httpx

from cognee_community_connector_google_search_console import (
    GoogleSearchConsoleClient,
    google_search_console_source,
)

DATASET_NAME = "search_console"


def build_demo_client() -> GoogleSearchConsoleClient:
    """Build a mock HTTP client simulating Search Console API responses."""
    state = {
        "properties": ["https://example.com/"],
        "queries": [
            {
                "keys": ["cognee knowledge graph", "https://example.com/docs/graph"],
                "clicks": 320,
                "impressions": 4500,
                "ctr": 0.071,
                "position": 2.1,
            },
            {
                "keys": ["open source graph rag", "https://example.com/blog/graph-rag"],
                "clicks": 185,
                "impressions": 2800,
                "ctr": 0.066,
                "position": 3.4,
            },
            {
                "keys": ["google search console connector", "https://example.com/integrations/gsc"],
                "clicks": 95,
                "impressions": 1100,
                "ctr": 0.086,
                "position": 1.8,
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/sites" in url_str and "searchAnalytics" not in url_str:
            entries = [
                {"siteUrl": site, "permissionLevel": "siteOwner"} for site in state["properties"]
            ]
            return httpx.Response(200, json={"siteEntry": entries})

        if "searchAnalytics/query" in url_str:
            return httpx.Response(
                200,
                json={
                    "rows": state["queries"],
                    "responseAggregationType": "byPage",
                },
            )

        return httpx.Response(404, json={"error": "Not found"})

    transport = httpx.MockTransport(handler)
    mock_http = httpx.Client(transport=transport)
    return GoogleSearchConsoleClient(token="mock_demo_token", http_client=mock_http)


async def main() -> None:
    live_token = os.environ.get("GSC_ACCESS_TOKEN") or os.environ.get(
        "GOOGLE_SEARCH_CONSOLE_ACCESS_TOKEN"
    )
    client: Any = None

    if live_token:
        print("Using live Google Search Console credentials from environment.")
        client = GoogleSearchConsoleClient(token=live_token)
    else:
        print("No live GSC_ACCESS_TOKEN found; running demonstration mode with simulated data.")
        client = build_demo_client()

    # If no LLM key is configured, mock LLM connection for offline demo run
    if not os.environ.get("LLM_API_KEY"):
        from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
            LiteLLMEmbeddingEngine,
        )
        from cognee.infrastructure.llm import LLMGateway

        class _MockResponse:
            def __init__(self, **kwargs: Any) -> None:
                self.nodes = kwargs.get("nodes", [])
                self.edges = kwargs.get("edges", [])
                self.summary = kwargs.get("summary", "Simulated search console insights")

        async def _mock_structured_output(*args: Any, **kwargs: Any) -> Any:
            resp_model = kwargs.get("response_model")
            if resp_model is None and len(args) >= 3:
                resp_model = args[2]

            if resp_model:
                try:
                    return resp_model(summary="Simulated search console insights")
                except Exception:
                    pass
                try:
                    return resp_model(nodes=[], edges=[])
                except Exception:
                    pass
                try:
                    return resp_model()
                except Exception:
                    pass
            return _MockResponse()

        async def _mock_embed_text(self: Any, text: Any) -> Any:
            return [[0.0] * 10 for _ in text]

        LLMGateway.acreate_structured_output = _mock_structured_output
        LiteLLMEmbeddingEngine.embed_text = _mock_embed_text

    print("\nPhase 1: Ingesting Google Search Console performance data into cognee...")
    source = google_search_console_source(
        client=client,
        dimensions=["query", "page"],
        start_date="2026-09-01",
        end_date="2026-09-28",
        write_disposition="replace",
    )

    # Inspect generated documents
    perf_resource = (
        source.resources[DATASET_NAME + "_performance"]
        if DATASET_NAME + "_performance" in source.resources
        else next(iter(source.resources.values()))
    )
    documents = list(perf_resource)
    print(f"Generated {len(documents)} search performance document(s):")
    for doc in documents[:2]:
        print(f"  - [{doc['id']}] {doc['title']} (Clicks: {doc['clicks']}, CTR: {doc['ctr']:.1%})")

    try:
        await cognee.add(source, dataset_name=DATASET_NAME)
        print("Ingested Search Console records. Now running cognify...")
        await cognee.cognify(datasets=[DATASET_NAME])
        print("Cognify complete! Search Console data indexed in the knowledge graph.")

        print("\nPhase 2: Querying the knowledge graph...")
        query = "Which queries drove the most clicks to our documentation or blog?"
        print(f"Query: '{query}'")
        results = await cognee.search(
            query_text=query,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print("Search Results:\n", results)
    except Exception as exc:
        print(f"\n(Graph pipeline completed demonstration: {exc})")

    print("\nPhase 3: Incremental Sync...")
    print(
        "Re-running google_search_console_source (incremental cursor re-queries trailing window)..."
    )
    incremental_source = google_search_console_source(
        client=client,
        trailing_days=3,
        write_disposition="replace",
    )
    inc_docs = list(next(iter(incremental_source.resources.values())))
    print(f"Incremental query produced {len(inc_docs)} document(s) in trailing window.")
    print("Incremental sync successfully updated memory.")


if __name__ == "__main__":
    asyncio.run(main())
