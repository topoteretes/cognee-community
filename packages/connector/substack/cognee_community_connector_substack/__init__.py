"""cognee-community-connector-substack

Substack data-source connector for cognee: sync a Substack newsletter into
memory via the public RSS feed.

Exports:
    substack_source  -- the main dlt source factory to hand to cognee.remember()
"""

from cognee_community_connector_substack.substack import substack_source

__all__ = ["substack_source"]
