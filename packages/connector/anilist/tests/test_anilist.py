"""Unit + pipeline; fake dicts shaped like live GraphQL response."""
import pytest, dlt
from cognee_community_connector_anilist.anilist import _build_row

def test_exact_row_shape_and_stable_id():
    entry={"media":{"id":21,"title":{"romaji":"One","english":"ONE PIECE"},"siteUrl":"https://anilist.co/anime/21"},"status":"CURRENT","score":9,"progress":1070,"notes":"great"}
    row=_build_row(entry)
    assert row=={"id":"anilist:21","url":"https://anilist.co/anime/21","title":"ONE PIECE","content":row["content"]}
    assert set(row.keys())=={"id","url","title","content"}

def test_title_prefers_english():
    entry={"media":{"id":1,"title":{"romaji":"R","english":"E"},"siteUrl":"u"},"status":"COMPLETED","score":0,"progress":0}
    assert _build_row(entry)["title"]=="E"

def test_content_markdown_full():
    entry={"media":{"id":99,"title":{"romaji":"T"},"siteUrl":"https://a/99","description":"desc","genres":["Action"],"studios":{"nodes":[{"name":"S1"}]},"averageScore":8.5},"status":"WATCHING","score":7,"progress":12,"notes":"n"}
    r=_build_row(entry)
    assert "## T" in r["content"]
    assert "Status: watching" in r["content"]
    assert "My score: 7/10" in r["content"]
    assert "### Synopsis" in r["content"]
    assert "desc" in r["content"]
    assert "Genres: Action" in r["content"]
    assert "AniList avg: 8.5" in r["content"]

def test_status_mapping():
    for s,l in {"CURRENT":"watching","PLANNING":"plan to watch","COMPLETED":"completed","DROPPED":"dropped","PAUSED":"paused","REPEATING":"rewatching"}.items():
        assert f"Status: {l}" in _build_row({"media":{"id":1,"title":{"romaji":"X"},"url":"u"},"status":s,"score":0,"progress":0})["content"]

def test_defensive_null_description():
    entry={"media":{"id":55,"title":{"romaji":"Z"},"url":"u"},"status":"COMPLETED","score":0,"progress":0}
    r=_build_row(entry)
    assert "### Synopsis" not in r["content"]

def test_pipeline_sync_edit_forget(tmp_path, monkeypatch):
    import dlt
    from cognee_community_connector_anilist.anilist import anilist_source

    def make_entry(mid, title, score):
        return {
            "media": {
                "id": mid,
                "title": {"romaji": title, "english": title},
                "siteUrl": f"https://anilist.co/anime/{mid}",
                "description": "syn",
                "genres": [],
                "studios": {"nodes": []},
                "averageScore": 8.0,
            },
            "status": "CURRENT",
            "score": score,
            "progress": 10,
            "updatedAt": "2024-01-01T00:00:00Z",
            "notes": "",
        }

    def make_payload(entries):
        return {
            "data": {
                "MediaListCollection": {
                    "lists": [{"name": "Watching", "entries": entries}],
                    "hasNextChunk": False,
                }
            }
        }

    class FR:
        def __init__(self, d):
            self.status_code = 200
            self.d = d

        def json(self):
            return self.d

        def raise_for_status(self):
            pass

    # run 1: two entries; run 2: first edited (score 9->10), second deleted
    payloads = [
        make_payload([make_entry(21, "ONE PIECE", 9), make_entry(1, "Cowboy Bebop", 8)]),
        make_payload([make_entry(21, "ONE PIECE", 10)]),
    ]

    class FC:
        def post(self, url, **kw):
            return FR(payloads.pop(0))

    monkeypatch.setattr("httpx.Client", lambda **kw: FC())
    db_path = (tmp_path / "al.db").as_posix()
    pl = dlt.pipeline(
        pipeline_name="al",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="al",
        pipelines_dir=str(tmp_path / "state"),
    )

    pl.run(anilist_source(username="test"))
    with pl.sql_client() as c:
        with c.execute_query("SELECT id, content FROM anilist_documents") as cur:
            rows = {r[0]: r[1] for r in cur.fetchall()}
    assert "anilist:21" in rows and "anilist:1" in rows

    pl.run(anilist_source(username="test"))
    with pl.sql_client() as c:
        with c.execute_query("SELECT id, content FROM anilist_documents") as cur:
            rows2 = {r[0]: r[1] for r in cur.fetchall()}
    assert "anilist:21" in rows2 and "My score: 10/10" in rows2["anilist:21"]
    assert "anilist:1" not in rows2
