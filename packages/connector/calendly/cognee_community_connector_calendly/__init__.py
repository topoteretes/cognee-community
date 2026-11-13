"""Calendly data-source connector for cognee.

Exposes Calendly event data as a dlt source for cognee's ingestion pipeline.
"""

from cognee_community_connector_calendly.calendly import calendly_source

__all__ = ["calendly_source"]
