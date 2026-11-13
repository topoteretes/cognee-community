"""Example demonstration: Ingesting Federal Reserve Economic Data into Cognee."""

import os
import sys
from pathlib import Path

# Ensure local package is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cognee_community_connector_fred import FredRenderOptions, fred_source

try:
    import cognee
except ImportError:
    cognee = None


async def main():
    api_key = os.getenv("FRED_API_KEY", "DEMO_KEY")

    series_to_track = [
        "GDP",  # Gross Domestic Product
        "UNRATE",  # Civilian Unemployment Rate
        "CPIAUCSL",  # Consumer Price Index (Inflation)
        "FEDFUNDS",  # Effective Federal Funds Rate
    ]

    print(f">> Initializing FRED Data Connector for series: {series_to_track}...")
    source = fred_source(
        api_key=api_key,
        series_ids=series_to_track,
        options=FredRenderOptions(max_observations=5, include_notes=True),
    )

    if cognee is not None:
        print(">> Ingesting FRED series into Cognee graph & vector memory...")
        await cognee.remember(
            source,
            dataset_name="us_macroeconomic_data",
        )
        print("\n[SUCCESS] Macroeconomic series remembered in Cognee graph and vector memory!")
    else:
        print("[INFO] Running in inspection mode (Cognee core optional dependency not present):")
        docs = []
        if isinstance(source, list) and len(source) > 0 and callable(source[0]):
            docs = list(source[0]())
        elif isinstance(source, list):
            docs = source
        else:
            try:
                docs = list(source)
            except Exception:
                docs = []

        print(f">> Generated {len(docs)} documents:")
        for d in docs:
            print(f"   * {d['id']} - {d['title']} ({d.get('units')})")

        print(
            "\n+---------------+------------------------------------+-------------+--------------+"
        )
        print("| Series ID     | Indicator Title                    | Latest Date | Observations |")
        print("+---------------+------------------------------------+-------------+--------------+")
        for doc in docs:
            s_id = str(doc.get("series_id", ""))[:13].ljust(13)
            title = str(doc.get("title", "").replace("FRED: ", ""))[:36].ljust(36)
            latest = str(doc.get("latest_observation_date", "N/A"))[:11].ljust(11)
            count = str(doc.get("observation_count", 0)).rjust(12)
            print(f"| {s_id} | {title} | {latest} | {count} |")
        print(
            "+---------------+--------------------------------------+-------------+--------------+\n"
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
