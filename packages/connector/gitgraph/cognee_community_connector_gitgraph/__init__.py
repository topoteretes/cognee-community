"""Git history, Python symbols and CI failures as a cognee data-source connector.

Cognee remembers documents, but a codebase's most useful memory is the history
around the code: who changed what, which symbols a commit touched, and which CI
failures followed. This package turns that history into a dlt source cognee can
ingest, and computes the blast radius of a pull request from the resulting graph.
"""

from cognee_community_connector_gitgraph.blast import blast_radius
from cognee_community_connector_gitgraph.links import link_rows
from cognee_community_connector_gitgraph.sample import SampleRepo, build_sample_repo
from cognee_community_connector_gitgraph.source import gitgraph_source

__all__ = ["SampleRepo", "blast_radius", "build_sample_repo", "gitgraph_source", "link_rows"]
__version__ = "0.1.0"
