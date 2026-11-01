"""Example: Ingesting Federal Reserve Economic Data (FRED) into Cognee memory."""

import asyncio
import os
import sys
from pathlib import Path

# Enable direct execution without requiring pip install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
        "CPIAUCSL",  # Consumer Price Index (Inflation)
        "FEDFUNDS",  # Federal Funds Effective Rate
    ]

    print("======================================================================")
    print(">> COGNEE COMMUNITY CONNECTOR: FRED (Federal Reserve Economic Data)")
    print(f">> Tracking Indicators: {', '.join(series_to_track)}")
    print("======================================================================\n")

    options = FredRenderOptions(
        max_observations=20,
        include_notes=True,
    )

    source = fred_source(
        api_key=fred_api_key,
        series_ids=series_to_track,
        options=options,
    )

    if cognee is not None:
        print(">> Handing source to Cognee AI Knowledge Graph Engine...")
        await cognee.remember(
            source,
            dataset_name="us_macroeconomic_data",
        )
        print("\n[SUCCESS] Macroeconomic series remembered in Cognee graph and vector memory!")
    else:
        print("[INFO] Running in inspection mode (Cognee core optional dependency not present):")
        docs = []
        if isinstance(source, list) and len(source) > 0 and hasattr(source[0], "__call__"):
            docs = list(source[0]())
        elif isinstance(source, list):
            docs = source
        else:
            try:
                docs = list(source)
            except Exception:
                docs = []

        if not docs:
            print("\n[NOTE] No documents returned. When running against live FRED endpoints,")
            print("please provide a valid 32-character FRED API key via:")
            print("  export FRED_API_KEY='your_32_character_api_key'")
            print("Get a free instant key at: https://fred.stlouisfed.org/docs/api/api_key.html")
            return

        print(f"\n+---------------+--------------------------------------+-------------+--------------+")
        print(f"| Series ID     | Indicator Title                      | Latest Date | Observations |")
        print(f"+---------------+--------------------------------------+-------------+--------------+")
        for doc in docs:
            s_id = doc.get("series_id", "")[:13].ljust(13)
            title = doc.get("title", "")[:36].ljust(36)
            latest = str(doc.get("latest_observation_date", "N/A"))[:11].ljust(11)
            count = str(doc.get("observation_count", 0)).rjust(12)
            print(f"| {s_id} | {title} | {latest} | {count} |")
        print(f"+---------------+--------------------------------------+-------------+--------------+\n")

        print(f"Document Snapshot [ID: {docs[0]['id']}]:")
        print("-" * 50)
        print(docs[0]["content"][:350] + "...\n")


if __name__ == "__main__":
    asyncio.run(main())
