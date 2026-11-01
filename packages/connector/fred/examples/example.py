"""Example: Ingesting Federal Reserve Economic Data (FRED) into Cognee memory."""

import asyncio
import os

from cognee_community_connector_fred import FredRenderOptions, fred_source

try:
    import cognee
except ImportError:
    cognee = None


async def main():
    fred_api_key = os.getenv("FRED_API_KEY", "DEMO_KEY")

    # Target key macroeconomic indicators
    series_to_track = [
        "GDP",       # Gross Domestic Product
        "UNRATE",    # Civilian Unemployment Rate
        "CPIAUCSL",  # Consumer Price Index for All Urban Consumers (Inflation)
        "FEDFUNDS",  # Federal Funds Effective Rate
    ]

    print(f"Initializing FRED connector for series: {series_to_track}...")

    options = FredRenderOptions(
        max_observations=30,
        include_notes=True,
    )

    source = fred_source(
        api_key=fred_api_key,
        series_ids=series_to_track,
        options=options,
    )

    if cognee is not None:
        print("Ingesting economic series into Cognee AI memory...")
        await cognee.remember(
            source,
            dataset_name="us_macroeconomic_data",
        )
        print("FRED economic data successfully ingested into Cognee memory!")
    else:
        print("Cognee not installed. Inspecting generated source documents directly:")
        # If source is generator or dlt source
        if isinstance(source, list) and hasattr(source[0], "__call__"):
            docs = list(source[0]())
        elif isinstance(source, list):
            docs = source
        else:
            docs = list(source)

        for doc in docs:
            print(f"\nDocument ID: {doc['id']}")
            print(f"Title: {doc['title']}")
            print(f"Latest Date: {doc.get('latest_observation_date')}")
            print(f"Preview:\n{doc['content'][:250]}...\n")


if __name__ == "__main__":
    asyncio.run(main())
