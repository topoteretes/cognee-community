import pytest

from cognee_community_connector_hackernews.hackernews import (
    HN_SOURCE_NAME,
    hackernews_source,
    iter_hackernews_items,
)

FAKE_TOP_STORIES = [1001, 1002]

FAKE_ITEMS = {
    "https://hacker-news.firebaseio.com/v0/topstories.json": FAKE_TOP_STORIES,
    "https://hacker-news.firebaseio.com/v0/item/1001.json": {
        "id": 1001,
        "type": "story",
        "by": "pg",
        "time": 1700000000,
        "title": "Show HN: Cognee Graph Memory",
        "url": "https://cognee.ai",
        "score": 350,
        "descendants": 2,
        "kids": [2001],
    },
    "https://hacker-news.firebaseio.com/v0/item/2001.json": {
        "id": 2001,
        "type": "comment",
        "by": "sama",
        "time": 1700000500,
        "text": "This is great. Really helps cognitive architectures.",
    },
    "https://hacker-news.firebaseio.com/v0/item/1002.json": {
        "id": 1002,
        "type": "story",
        "by": "dang",
        "time": 1700001000,
        "title": "Ask HN: Favorite developer tools?",
        "text": "<p>What tools make you 10x more productive?</p>",
        "score": 120,
        "descendants": 0,
    },
}


def test_iter_hackernews_items():
    def mock_fetch(url):
        return FAKE_ITEMS.get(url)

    stories = list(iter_hackernews_items(limit=2, include_top_comments=1, fetch_fn=mock_fetch))
    assert len(stories) == 2

    s1 = stories[0]
    assert s1["item_id"] == 1001
    assert s1["title"] == "Show HN: Cognee Graph Memory"
    assert s1["author"] == "pg"
    assert s1["score"] == 350
    assert "sama:" in s1["content"]
    assert "cognitive architectures" in s1["content"]

    s2 = stories[1]
    assert s2["item_id"] == 1002
    assert "What tools make you 10x more productive?" in s2["content"]


def test_hackernews_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")

    def mock_fetch(url):
        return FAKE_ITEMS.get(url)

    db_path = tmp_path / "hn.db"
    pipeline = dlt.pipeline(
        pipeline_name="hn_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="hn_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = hackernews_source(limit=2, fetch_fn=mock_fetch)
    assert getattr(source, "cognee_document_source", None) == HN_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT item_id, author, score FROM hackernews_stories ORDER BY item_id")
        assert len(rows) == 2
        assert rows[0][0] == 1001
        assert rows[0][1] == "pg"
