"""Example demonstration: Ingesting DigitalOcean cloud infrastructure into Cognee."""

import os
import sys
from pathlib import Path

# Ensure local package is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cognee_community_connector_digitalocean import (
    DigitalOceanRenderOptions,
    digitalocean_source,
)

try:
    import cognee
except ImportError:
    cognee = None


async def main():
    token = os.getenv("DIGITALOCEAN_TOKEN", "dop_v1_demo_token")

    print(">> Initializing DigitalOcean Data Connector...")
    source = digitalocean_source(
        token=token,
        options=DigitalOceanRenderOptions(
            include_droplets=True,
            include_apps=True,
            include_databases=True,
            include_kubernetes=True,
            include_projects=True,
            redact_secrets=True,
        ),
    )

    if cognee is not None:
        print(">> Ingesting DigitalOcean resources into Cognee knowledge graph...")
        await cognee.remember(
            source,
            dataset_name="digitalocean_cloud_infrastructure",
        )
        print("\n[SUCCESS] Cloud infrastructure remembered in Cognee AI memory!")
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
            print(f"   * [{d.get('type', 'resource').upper()}] {d['id']} - {d['title']}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
