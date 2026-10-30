# OpenStreetMap Places Connector for Cognee

A `dlt` data-source connector that ingests local places, amenities (cafes, libraries, parks, restaurants), addresses, opening hours, and accessibility information from OpenStreetMap via the Overpass API into Cognee memory.

Part of the **Cognee Mergetober Hackathon** (`#6187`).

---

## Features

- **Zero API Keys Required:** Uses the public OpenStreetMap Overpass API without needing private tokens or account credentials.
- **Bounding Box & Amenity Filtering:** Filter by latitude/longitude bounding box and specific amenities (`cafe`, `library`, `park`, `pharmacy`, etc.) or custom Overpass QL queries.
- **Rich Structured Documents:** Extracts place names, coordinates, addresses (house numbers, street, city, postal code), opening hours, wheelchair accessibility, and contact links into clean Markdown.
- **Full Snapshot & Replace:** Uses `write_disposition="replace"` so memory updates cleanly without stale duplicates.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.
- **Attribution Included:** Automatically includes required OpenStreetMap contributor licensing attribution (ODbL 1.0).

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_openstreetmap import openstreetmap_source

async def main():
    # 1. Initialize OpenStreetMap source with a bounding box (e.g. Central London)
    # Bounding box format: (min_lat, min_lon, max_lat, max_lon)
    source = openstreetmap_source(
        bbox=(51.500, -0.130, 51.515, -0.110),
        amenities=["cafe", "library", "park"],
        limit=25,
    )

    # 2. Add to Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search local places in Cognee
    results = await cognee.search("Which cafes or libraries are tagged as wheelchair accessible?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic offline test suite:

```bash
pytest packages/connector/openstreetmap/tests
```
