"""OpenStreetMap Places and POI data-source connector for Cognee.

Ingests local places, amenities (cafes, libraries, parks, etc.), addresses, opening hours,
and accessibility metadata from OpenStreetMap via the Overpass API into Cognee memory.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple
import dlt
import httpx

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger(__name__)

OVERPASS_DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
OSM_ATTRIBUTION = "Data © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"


class OpenStreetMapClient:
    """Client for querying the OpenStreetMap Overpass API."""

    def __init__(
        self,
        endpoint: str = OVERPASS_DEFAULT_ENDPOINT,
        http_client: Optional[Any] = None,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint
        self._http_client = http_client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "CogneeCommunityOpenStreetMapConnector/0.1.0"},
        )

    def build_query(
        self,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        amenities: Optional[List[str]] = None,
        custom_query: Optional[str] = None,
        limit: int = 100,
    ) -> str:
        """Construct an Overpass QL query string."""
        if custom_query:
            return custom_query

        amenity_filter = "|".join(amenities) if amenities else "cafe|restaurant|library|park|pharmacy"
        
        if bbox:
            min_lat, min_lon, max_lat, max_lon = bbox
            bbox_str = f"({min_lat},{min_lon},{max_lat},{max_lon})"
        else:
            bbox_str = ""

        query = f"""[out:json][timeout:25];
(
  node["amenity"~"{amenity_filter}"]{bbox_str};
  way["amenity"~"{amenity_filter}"]{bbox_str};
);
out center {limit};"""
        return query

    def fetch_places(
        self,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        amenities: Optional[List[str]] = None,
        custom_query: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetch matching OpenStreetMap elements from Overpass."""
        query = self.build_query(bbox=bbox, amenities=amenities, custom_query=custom_query, limit=limit)
        try:
            resp = self._http_client.post(self.endpoint, data={"data": query})
            resp.raise_for_status()
            data = resp.json()
            return data.get("elements", [])
        except Exception as exc:
            logger.error("Failed to fetch OpenStreetMap data from Overpass: %s", exc)
            raise


def _format_address(tags: Dict[str, Any]) -> str:
    """Extract and format address from OpenStreetMap tags."""
    parts = []
    street = tags.get("addr:street")
    housenumber = tags.get("addr:housenumber")
    if street and housenumber:
        parts.append(f"{housenumber} {street}")
    elif street:
        parts.append(street)

    city = tags.get("addr:city") or tags.get("addr:suburb")
    postcode = tags.get("addr:postcode")
    if postcode and city:
        parts.append(f"{city}, {postcode}")
    elif city:
        parts.append(city)

    country = tags.get("addr:country")
    if country:
        parts.append(country)

    return ", ".join(parts) if parts else "Not specified"


def _element_to_document(element: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert an Overpass element into a structured Markdown document row."""
    elem_type = element.get("type", "node")
    elem_id = element.get("id")
    if not elem_id:
        return None

    tags = element.get("tags", {})
    name = tags.get("name") or tags.get("name:en") or f"Unnamed {tags.get('amenity', 'place')}"
    amenity = tags.get("amenity") or tags.get("shop") or tags.get("tourism") or tags.get("leisure") or "place"

    # Coordinates
    lat = element.get("lat") or element.get("center", {}).get("lat")
    lon = element.get("lon") or element.get("center", {}).get("lon")
    coord_str = f"{lat:.5f}, {lon:.5f}" if lat is not None and lon is not None else "Unknown"

    url = f"https://www.openstreetmap.org/{elem_type}/{elem_id}"
    address = _format_address(tags)
    opening_hours = tags.get("opening_hours", "Not specified")
    wheelchair = tags.get("wheelchair", "Unknown")
    phone = tags.get("phone") or tags.get("contact:phone", "Not specified")
    website = tags.get("website") or tags.get("contact:website", "Not specified")

    lines = [
        f"# {name} ({amenity.capitalize()})",
        f"**Type:** {amenity.capitalize()} ({elem_type.upper()} #{elem_id})",
        f"**Coordinates:** {coord_str}",
        f"**OpenStreetMap Link:** [{elem_type}/{elem_id}]({url})",
        "",
        "## Location & Address",
        f"**Address:** {address}",
        "",
        "## Practical Information",
        f"- **Opening Hours:** {opening_hours}",
        f"- **Wheelchair Accessibility:** {wheelchair}",
        f"- **Phone:** {phone}",
        f"- **Website:** {website}",
    ]

    # Additional notable tags
    notable_keys = [k for k in tags if k not in {
        "name", "name:en", "amenity", "addr:street", "addr:housenumber",
        "addr:city", "addr:postcode", "addr:country", "opening_hours",
        "wheelchair", "phone", "contact:phone", "website", "contact:website"
    }]
    if notable_keys:
        lines.append("")
        lines.append("## Details & Tags")
        for k in sorted(notable_keys):
            lines.append(f"- **{k}:** {tags[k]}")

    lines.append("")
    lines.append("---")
    lines.append(f"*{OSM_ATTRIBUTION}*")

    content = "\n".join(lines)
    return {
        "id": f"osm_{elem_type}_{elem_id}",
        "url": url,
        "title": f"{name} ({amenity.capitalize()})",
        "content": content,
    }


@dlt.source(name="openstreetmap_source")
def openstreetmap_source(
    bbox: Optional[Tuple[float, float, float, float]] = None,
    amenities: Optional[List[str]] = None,
    custom_query: Optional[str] = None,
    limit: int = 100,
    client: Optional[OpenStreetMapClient] = None,
) -> Any:
    """DLT data-source for ingesting OpenStreetMap places into Cognee."""
    osm_client = client or OpenStreetMapClient()

    @dlt.resource(
        name="places",
        write_disposition="replace",
        primary_key="id",
    )
    def places() -> Generator[Dict[str, Any], None, None]:
        elements = osm_client.fetch_places(
            bbox=bbox,
            amenities=amenities,
            custom_query=custom_query,
            limit=limit,
        )
        for elem in elements:
            doc = _element_to_document(elem)
            if doc:
                yield doc

    resource = places()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "osm_places")
    return resource
