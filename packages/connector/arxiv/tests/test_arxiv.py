import pytest

from cognee_community_connector_arxiv.arxiv import (
    ARXIV_SOURCE_NAME,
    arxiv_source,
    fetch_arxiv_papers,
    parse_arxiv_feed,
)

SAMPLE_ARXIV_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title type="html">arXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <published>2024-01-20T18:00:00Z</published>
    <updated>2024-01-21T10:00:00Z</updated>
    <title>Graph Neural Networks for Knowledge Bases</title>
    <summary>We introduce a novel relational GNN architecture for entity extraction and cognitive memory.</summary>
    <author>
      <name>Alice Smith</name>
    </author>
    <author>
      <name>Bob Jones</name>
    </author>
    <primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.AI"/>
    <link title="pdf" href="http://arxiv.org/pdf/2401.12345v1" type="application/pdf"/>
  </entry>
</feed>
"""


def test_parse_arxiv_feed():
    papers = parse_arxiv_feed(SAMPLE_ARXIV_XML)
    assert len(papers) == 1
    p = papers[0]
    assert p["arxiv_id"] == "2401.12345v1"
    assert p["title"] == "Graph Neural Networks for Knowledge Bases"
    assert p["authors"] == "Alice Smith, Bob Jones"
    assert p["primary_category"] == "cs.AI"
    assert p["pdf_url"] == "http://arxiv.org/pdf/2401.12345v1"
    assert "Graph Neural Networks for Knowledge Bases" in p["content"]


def test_fetch_arxiv_papers_with_mock():
    called_params = []

    def mock_fetch(params):
        called_params.append(params)
        return SAMPLE_ARXIV_XML

    papers = list(
        fetch_arxiv_papers(
            category="cs.AI",
            max_results=1,
            fetch_fn=mock_fetch,
        )
    )
    assert len(papers) == 1
    assert "cat:cs.AI" in called_params[0]["search_query"]
    assert called_params[0]["max_results"] == 1


def test_arxiv_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")

    def mock_fetch(params):
        return SAMPLE_ARXIV_XML

    db_path = tmp_path / "arxiv.db"
    pipeline = dlt.pipeline(
        pipeline_name="arxiv_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="arxiv_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = arxiv_source(query="knowledge graph", fetch_fn=mock_fetch)
    assert getattr(source, "cognee_document_source", None) == ARXIV_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT arxiv_id, title FROM arxiv_papers")
        assert len(rows) == 1
        assert rows[0][0] == "2401.12345v1"
