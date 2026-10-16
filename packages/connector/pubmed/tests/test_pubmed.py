"""Offline coverage for PubMed parsing, incremental sync, and deletion propagation."""

import gzip
from copy import deepcopy

import pytest

from cognee_community_connector_pubmed.pubmed import (
    PubMedClient,
    _parse_articles,
    pubmed_source,
    sync_articles,
)

ARTICLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal>
          <JournalIssue><PubDate><Year>2026</Year><Month>Sep</Month><Day>20</Day></PubDate></JournalIssue>
          <Title>Journal of Reliable Connectors</Title>
        </Journal>
        <ArticleTitle>Agentic <i>retrieval</i> for biomedicine</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">The first section.</AbstractText>
          <AbstractText Label="METHODS">The <b>second</b> section.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><ForeName>Ada</ForeName><LastName>Lovelace</LastName></Author>
          <Author><CollectiveName>Connector Study Group</CollectiveName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
      <MeshHeadingList>
        <MeshHeading>
          <DescriptorName>Information Storage and Retrieval</DescriptorName>
        </MeshHeading>
      </MeshHeadingList>
      <KeywordList><Keyword>knowledge graph</Keyword></KeywordList>
    </MedlineCitation>
    <PubmedData>
      <History>
        <PubMedPubDate PubStatus="entrez">
          <Year>2026</Year><Month>9</Month><Day>21</Day>
        </PubMedPubDate>
      </History>
      <ArticleIdList>
        <ArticleId IdType="pubmed">{pmid}</ArticleId>
        <ArticleId IdType="doi">10.1000/{pmid}</ArticleId>
        <ArticleId IdType="pmc">PMC{pmid}</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


class FakeResponse:
    def __init__(self, *, content=b"", json_payload=None):
        self.content = content
        self._json_payload = json_payload

    def raise_for_status(self):
        return None

    def json(self):
        return deepcopy(self._json_payload)


class FakeSession:
    """Requests-compatible ESearch/EFetch/deleted-feed fake."""

    def __init__(self, pmids, *, deleted=(), omitted=(), count=None):
        self.pmids = list(pmids)
        self.deleted = set(deleted)
        self.omitted = set(omitted)
        self.count = len(self.pmids) if count is None else count
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, deepcopy(params), timeout))
        if url.endswith("esearch.fcgi"):
            return FakeResponse(
                json_payload={"esearchresult": {"count": str(self.count), "idlist": self.pmids}}
            )
        if url.endswith("efetch.fcgi"):
            records = [
                ARTICLE_XML.format(pmid=pmid)
                for pmid in params["id"].split(",")
                if pmid not in self.omitted
            ]
            inner = "".join(
                record.split("<PubmedArticleSet>", 1)[1].rsplit("</PubmedArticleSet>", 1)[0]
                for record in records
            )
            return FakeResponse(content=f"<PubmedArticleSet>{inner}</PubmedArticleSet>".encode())
        if url.endswith("deleted.pmids.gz"):
            payload = "\n".join(sorted(self.deleted)).encode("ascii")
            return FakeResponse(content=gzip.compress(payload))
        raise AssertionError(f"unexpected URL: {url}")


def _client(session):
    return PubMedClient(session=session, request_interval=0)


def test_parse_article_preserves_structured_abstract_and_metadata():
    row = _parse_articles(ARTICLE_XML.format(pmid="123").encode())[0]

    assert row == {
        "id": "123",
        "title": "Agentic retrieval for biomedicine",
        "content": (
            "Agentic retrieval for biomedicine\n\n"
            "BACKGROUND: The first section.\n\nMETHODS: The second section."
        ),
        "abstract": "BACKGROUND: The first section.\n\nMETHODS: The second section.",
        "authors": ["Ada Lovelace", "Connector Study Group"],
        "journal": "Journal of Reliable Connectors",
        "publication_date": "2026-Sep-20",
        "entrez_date": "2026/09/21",
        "doi": "10.1000/123",
        "pmc_id": "PMC123",
        "publication_types": ["Journal Article"],
        "mesh_terms": ["Information Storage and Retrieval"],
        "keywords": ["knowledge graph"],
        "url": "https://pubmed.ncbi.nlm.nih.gov/123/",
        "_deleted": False,
    }


def test_initial_sync_searches_once_batches_fetches_and_records_cursor():
    session = FakeSession(["101", "102", "103"])
    state = {}

    rows = list(
        sync_articles(
            _client(session),
            "agentic retrieval",
            state,
            start_date="2026-09-01",
            end_date="2026-09-21",
            batch_size=2,
        )
    )

    assert [row["id"] for row in rows] == ["101", "102", "103"]
    assert state == {
        "query": "agentic retrieval",
        "last_edat": "2026/09/21",
        "known_ids": ["101", "102", "103"],
    }
    search_calls = [call for call in session.calls if call[0].endswith("esearch.fcgi")]
    fetch_calls = [call for call in session.calls if call[0].endswith("efetch.fcgi")]
    assert len(search_calls) == 1
    assert search_calls[0][1]["retmax"] == 10_000
    assert search_calls[0][1]["datetype"] == "edat"
    assert [call[1]["id"] for call in fetch_calls] == ["101,102", "103"]


def test_incremental_sync_refetches_cursor_day_but_only_fetches_new_pmids():
    session = FakeSession(["101", "102"])
    state = {
        "query": "agentic retrieval",
        "last_edat": "2026/09/21",
        "known_ids": ["101"],
    }

    rows = list(
        sync_articles(
            _client(session),
            "agentic retrieval",
            state,
            start_date="2026-01-01",
            end_date="2026-09-22",
        )
    )

    assert [row["id"] for row in rows] == ["102"]
    search = next(call for call in session.calls if call[0].endswith("esearch.fcgi"))
    assert search[1]["mindate"] == "2026/09/21"
    fetch = next(call for call in session.calls if call[0].endswith("efetch.fcgi"))
    assert fetch[1]["id"] == "102"
    assert state["known_ids"] == ["101", "102"]


def test_deleted_feed_emits_hard_delete_marker_and_forgets_known_pmid():
    session = FakeSession(["101"], deleted={"102", "999"})
    state = {
        "query": "agentic retrieval",
        "last_edat": "2026/09/21",
        "known_ids": ["101", "102"],
    }

    rows = list(
        sync_articles(
            _client(session),
            "agentic retrieval",
            state,
            start_date="2026-01-01",
            end_date="2026-09-22",
        )
    )

    assert rows == [{"id": "102", "_deleted": True}]
    assert state["known_ids"] == ["101"]


def test_deleted_feed_failure_does_not_advance_cursor_or_state():
    class FailingDeletedFeed(FakeSession):
        def get(self, url, params=None, timeout=None):
            if url.endswith("deleted.pmids.gz"):
                raise ConnectionError("feed unavailable")
            return super().get(url, params, timeout)

    state = {
        "query": "agentic retrieval",
        "last_edat": "2026/09/20",
        "known_ids": ["101"],
    }
    original = deepcopy(state)

    with pytest.raises(ConnectionError, match="feed unavailable"):
        list(
            sync_articles(
                _client(FailingDeletedFeed(["101", "102"])),
                "agentic retrieval",
                state,
                start_date="2026-01-01",
                end_date="2026-09-22",
            )
        )

    assert state == original


def test_efetch_omission_aborts_without_advancing_state():
    state = {}
    with pytest.raises(ValueError, match=r"EFetch omitted requested PMIDs: \['101'\]"):
        list(
            sync_articles(
                _client(FakeSession(["101"], omitted={"101"})),
                "agentic retrieval",
                state,
                start_date="2026-09-01",
                end_date="2026-09-22",
            )
        )
    assert state == {}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": ""}, "query must not be empty"),
        ({"batch_size": 0}, "batch_size"),
        ({"end_date": "2025-01-01"}, "must not be after"),
    ],
)
def test_invalid_sync_configuration_fails_before_network(kwargs, message):
    values = {
        "query": "agentic retrieval",
        "start_date": "2026-01-01",
        "end_date": "2026-09-22",
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        list(sync_articles(_client(FakeSession([])), state={}, **values))


def test_query_change_is_rejected_to_protect_persisted_state():
    state = {"query": "cancer", "known_ids": ["1"], "last_edat": "2026/09/01"}
    with pytest.raises(ValueError, match="query changed"):
        list(
            sync_articles(
                _client(FakeSession([])),
                "diabetes",
                state,
                start_date="2026-01-01",
                end_date="2026-09-22",
            )
        )


def test_search_over_service_limit_fails_instead_of_advancing_a_partial_corpus():
    client = _client(FakeSession([], count=10_001))
    with pytest.raises(ValueError, match="matched 10001 records"):
        client.search(
            "cancer",
            mindate="2026/09/01",
            maxdate="2026/09/22",
        )


def test_pubmed_source_has_merge_primary_key_delete_and_document_mode():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    resource = pubmed_source(
        "agentic retrieval",
        start_date="2026-09-01",
        end_date="2026-09-22",
        session=FakeSession([]),
        request_interval=0,
    )

    schema = resource.compute_table_schema()
    disposition = schema["write_disposition"]
    if isinstance(disposition, dict):
        disposition = disposition["disposition"]
    assert disposition == "merge"
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["columns"]["_deleted"]["hard_delete"] is True
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "pubmed"


def test_forget_on_delete_runs_through_real_dlt_merge(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    from dlt.common.runtime import run_context

    dlt_global = tmp_path / "dlt-global"
    dlt_global.mkdir()
    monkeypatch.setattr(run_context, "global_dir", lambda: str(dlt_global))
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")

    database = (tmp_path / "pubmed.sqlite").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_pubmed_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{database}"),
        dataset_name="literature",
        pipelines_dir=str(tmp_path / "state"),
    )
    source_kwargs = {
        "query": "agentic retrieval",
        "start_date": "2026-09-01",
        "end_date": "2026-09-22",
        "request_interval": 0,
    }

    pipeline.run(pubmed_source(session=FakeSession(["101", "102"]), **source_kwargs))
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM pubmed_articles")[0][0] == 2

    pipeline.run(pubmed_source(session=FakeSession(["101"], deleted={"102"}), **source_kwargs))
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM pubmed_articles ORDER BY id")
    assert rows == [("101",)]


def test_client_adds_identity_and_optional_api_key_to_eutils_calls():
    session = FakeSession([])
    client = PubMedClient(
        session=session,
        api_key="secret",
        email="researcher@example.com",
        tool="resume-project",
        request_interval=0,
    )

    client.search(
        "agentic retrieval",
        mindate="2026/09/01",
        maxdate="2026/09/22",
    )

    params = session.calls[0][1]
    assert params["api_key"] == "secret"
    assert params["email"] == "researcher@example.com"
    assert params["tool"] == "resume-project"
