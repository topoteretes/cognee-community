"""Tests — fake dicts shaped like live API, plus pipeline test."""
import pytest, dlt
from cognee_community_connector_chesscom.chesscom import _build_row, _normalize_result, _opening_name

def test_exact_row_shape_and_stable_id():
    game = {"uuid":"abc123","url":"https://chess.com/game/abc123","time_class":"Rapid","white":{"username":"me","rating":1500,"result":"win"},"black":{"username":"them","rating":1400,"result":"checkmated"},"eco":"https://www.chess.com/openings/Kings-Pawn-Opening","accuracies":{"white":95,"black":88},"end_time":1700000000}
    row = _build_row("me", game)
    assert row["id"] == "chesscom:abc123"
    assert row["url"] == "https://chess.com/game/abc123"
    assert row["title"] == "Rapid vs them (win)"
    assert set(row.keys()) == {"id","url","title","content"}

def test_title_format():
    game = {"uuid":"x","url":"u","time_class":"Blitz","white":{"username":"me","rating":1500,"result":"win"},"black":{"username":"opp","rating":1400,"result":"checkmated"}}
    row = _build_row("me", game)
    assert row["title"] == "Blitz vs opp (win)"

def test_content_markdown_sections():
    game = {"uuid":"x","url":"u","time_class":"Rapid","white":{"username":"me","rating":1500,"result":"win"},"black":{"username":"opp","rating":1400,"result":"checkmated"},"eco":"https://www.chess.com/openings/Kings-Pawn-Opening","accuracies":{"white":95},"end_time":1700000000,"pgn":"pgn1"}
    row = _build_row("me", game)
    assert "## Rapid vs opp (win)" in row["content"]
    assert "Result: Win" in row["content"]
    assert "My rating: 1500" in row["content"]
    assert "Opening: Kings Pawn Opening" in row["content"]
    assert "Accuracy: 95" in row["content"]
    assert "### PGN" in row["content"]

def test_result_mapping():
    assert _normalize_result("me", {"white":{"username":"me","rating":1500,"result":"win"},"black":{"username":"opp","rating":1400,"result":"checkmated"}}) == "win"
    assert _normalize_result("me", {"white":{"username":"opp","rating":1500,"result":"checkmated"},"black":{"username":"me","rating":1400,"result":"win"}}) == "win"
    assert _normalize_result("me", {"white":{"username":"me","rating":1500,"result":"resigned"},"black":{"username":"opp","rating":1400,"result":"win"}}) == "loss"
    assert _normalize_result("me", {"white":{"username":"opp","rating":1500,"result":"agreed"},"black":{"username":"me","rating":1400,"result":"agreed"}}) == "draw"

def test_defensive_null_accuracies_missing_eco():
    game = {"uuid":"x","url":"u","time_class":"Bullet","white":{"username":"me","rating":1500,"result":"win"},"black":{"username":"opp","rating":1400,"result":"checkmated"}}
    row = _build_row("me", game)
    assert "Accuracy:" not in row["content"]
    assert "Opening:" not in row["content"]

def test_pipeline_sync_edit_forget(tmp_path, monkeypatch):
    import dlt
    from cognee_community_connector_chesscom.chesscom import chesscom_source
    class FakeResp:
        def __init__(self, d): self.status_code = 200; self.headers = {"content-type":"application/json"}; self._d = d
        def json(self): return self._d
        def raise_for_status(self): pass
    class FakeClient:
        def __init__(self): self.base = "https://api.chess.com/pub"
        def get(self, url, headers=None, timeout=30.0):
            if "archives" in url:
                return FakeResp({"archives": [f"{self.base}/player/test/games/2025/10", f"{self.base}/player/test/games/2025/11"]})
            if "2025/10" in url:
                return FakeResp({"games":[{"uuid":"g1","url":"u1","time_class":"Rapid","white":{"username":"me","rating":1500,"result":"win"},"black":{"username":"opp","rating":1400,"result":"checkmated"},"eco":None,"accuracies":None,"end_time":1700000000,"pgn":"pgn1"}]})
            if "2025/11" in url:
                return FakeResp({"games":[{"uuid":"g2","url":"u2","time_class":"Blitz","white":{"username":"opp","rating":1500,"result":"checkmated"},"black":{"username":"me","rating":1400,"result":"win"},"eco":None,"accuracies":None,"end_time":1700000000,"pgn":"pgn2"}]})
            return FakeResp({"games":[]})
    fake = FakeClient()
    monkeypatch.setattr("httpx.Client", lambda **kw: fake)
    db_path = (tmp_path/"chess.db").as_posix()
    pl = dlt.pipeline(pipeline_name="chess", destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"), dataset_name="chess", pipelines_dir=str(tmp_path/"state"))
    pl.run(chesscom_source(username="test", months_back=2))
    with pl.sql_client() as c:
        with c.execute_query("SELECT id FROM chesscom_documents") as cur:
            ids = [r[0] for r in cur.fetchall()]
    assert "chesscom:g1" in ids and "chesscom:g2" in ids
