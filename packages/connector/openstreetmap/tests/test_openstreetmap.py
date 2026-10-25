"""Hermetic unit tests for OpenStreetMap connector."""

from typing import Any, Dict, List
import pytest
from cognee_community_connector_openstreetmap.openstreetmap import (
    OpenStreetMapClient,
    _element_to_document,
    _format_address,
    openstreetmap_source,
    OSM_ATTRIBUTION,
)

SAMPLE_NODE_ELEMENT: Dict[str, Any] = {
    "type": "node",
    "id": 12345678,
    "lat": 51.5074,
    "lon": -0.1278,
    "tags": {
        "name": "The Roastery Cafe",
        "amenity": "cafe",
        "addr:street": "Fleet Street",
        "addr:housenumber": "42",
        "addr:city": "London",
        "addr:postcode": "EC4Y 1AA",
        "addr:country": "UK",
        "opening_hours": "Mo-Fr 07:00-18:00; Sa 08:00-17:00",
        "wheelchair": "yes",
        "phone": "+44 20 7946 0123",
        "website": "https://theroastery.example.com",
        "cuisine": "coffee_shop",
        "outdoor_seating": "yes",
    },
}

SAMPLE_WAY_ELEMENT: Dict[str, Any] = {
    "type": "way",
    "id": 87654321,
    "center": {
        "lat": 51.5120,
        "lon": -0.1200,
    },
    "tags": {
        "name": "Lincoln's Inn Fields",
        "leisure": "park",
        "amenity": "park",
        "addr:city": "London",
        "wheelchair": "yes",
        "opening_hours": "dawn to dusk",
    },
}


class FakeResponse:
    def __init__(self, json_data: Dict[str, Any], status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return self._json_data


class FakeOverpassHttpClient:
    def __init__(self, elements: List[Dict[str, Any]]) -> None:
        self.elements = elements
        self.recorded_queries: List[str] = []

    def post(self, url: str, data: Dict[str, Any]) -> FakeResponse:
        self.recorded_queries.append(data.get("data", ""))
        return FakeResponse({"elements": self.elements})


def test_build_query_with_bbox_and_amenities():
    """Test Overpass QL query construction."""
    client = OpenStreetMapClient()
    bbox = (51.50, -0.13, 51.51, -0.12)
    query = client.build_query(bbox=bbox, amenities=["cafe", "library"], limit=50)

    assert "[out:json][timeout:25];" in query
    assert 'node["amenity"~"cafe|library"](51.5,-0.13,51.51,-0.12);' in query
    assert 'way["amenity"~"cafe|library"](51.5,-0.13,51.51,-0.12);' in query
    assert "out center 50;" in query


def test_format_address_full():
    """Test address formatting with full street, city, and postcode."""
    tags = {
        "addr:housenumber": "10",
        "addr:street": "Downing Street",
        "addr:city": "London",
        "addr:postcode": "SW1A 2AA",
        "addr:country": "UK",
    }
    address = _format_address(tags)
    assert address == "10 Downing Street, London, SW1A 2AA, UK"


def test_node_element_to_document():
    """Test converting a node element into a Cognee Markdown document."""
    doc = _element_to_document(SAMPLE_NODE_ELEMENT)
    assert doc is not None
    assert doc["id"] == "osm_node_12345678"
    assert doc["url"] == "https://www.openstreetmap.org/node/12345678"
    assert doc["title"] == "The Roastery Cafe (Cafe)"

    content = doc["content"]
    assert "# The Roastery Cafe (Cafe)" in content
    assert "**Coordinates:** 51.50740, -0.12780" in content
    assert "42 Fleet Street, London, EC4Y 1AA, UK" in content
    assert "Mo-Fr 07:00-18:00; Sa 08:00-17:00" in content
    assert "Wheelchair Accessibility:** yes" in content
    assert "https://theroastery.example.com" in content
    assert "cuisine:** coffee_shop" in content
    assert OSM_ATTRIBUTION in content


def test_way_element_to_document():
    """Test converting a way (polygon/park) into a Cognee document with center coordinates."""
    doc = _element_to_document(SAMPLE_WAY_ELEMENT)
    assert doc is not None
    assert doc["id"] == "osm_way_87654321"
    assert doc["title"] == "Lincoln's Inn Fields (Park)"
    assert "**Coordinates:** 51.51200, -0.12000" in doc["content"]
    assert "Wheelchair Accessibility:** yes" in doc["content"]


def test_dlt_source_execution():
    """Test executing openstreetmap_source with mocked Overpass HTTP client."""
    fake_client = FakeOverpassHttpClient([SAMPLE_NODE_ELEMENT, SAMPLE_WAY_ELEMENT])
    client = OpenStreetMapClient(http_client=fake_client)

    resource = openstreetmap_source(
        bbox=(51.50, -0.13, 51.51, -0.12),
        amenities=["cafe", "park"],
        limit=10,
        client=client,
    )

    items = list(resource)
    assert len(items) == 2
    assert items[0]["id"] == "osm_node_12345678"
    assert items[1]["id"] == "osm_way_87654321"
    assert len(fake_client.recorded_queries) == 1
