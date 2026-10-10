"""Chess.com dlt source (public API — no auth)."""
from __future__ import annotations
import os
import time
from typing import Any

import dlt
import httpx
try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except Exception:
    DOCUMENT_SOURCE_ATTR = "dlt_source"

API_ROOT = "https://api.chess.com/pub"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _get(client: httpx.Client, url: str) -> Any:
    time.sleep(1.0)
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(4):
        try:
            resp = client.get(url, headers=headers, timeout=30.0)
        except httpx.HTTPError as exc:
            if attempt == 3: raise exc
            time.sleep(1.0); continue
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            wait = float(ra) if ra and ra.isdigit() else 2.0
            time.sleep(wait); time.sleep(1.0)
            continue
        resp.raise_for_status()
        if "application/json" in resp.headers.get("content-type", ""):
            return resp.json()
        return resp.text or ""
    raise RuntimeError("request failed")


def _normalize_result(my_username: str, game: dict[str, Any]) -> str:
    white = game.get("white", {})
    black = game.get("black", {})
    my_color = "white" if white.get("username", "").lower() == my_username.lower() else "black"
    my_result = white.get("result") if my_color == "white" else black.get("result")
    if not my_result:
        return "unknown"
    my_result = my_result.lower()
    win_vals = {"win"}
    loss_vals = {"checkmated", "timeout", "resigned", "lose", "abandoned"}
    draw_vals = {"agreed", "repetition", "stalemate", "50move", "insufficient", "timevsinsufficient"}
    if my_result in win_vals: return "win"
    if my_result in loss_vals: return "loss"
    if my_result in draw_vals: return "draw"
    return my_result


def _opening_name(eco_url: str | None) -> str | None:
    if not eco_url: return None
    # eco like "https://www.chess.com/openings/Kings-Pawn-Opening"
    return eco_url.rstrip("/").split("/")[-1].replace("-", " ")


def _build_row(username: str, game: dict[str, Any]) -> dict[str, Any]:
    uuid = game.get("uuid", "")
    url = game.get("url", "")
    time_class = game.get("time_class", "Unknown")
    white = game.get("white", {})
    black = game.get("black", {})
    my_color = "white" if white.get("username", "").lower() == username.lower() else "black"
    opponent = black.get("username", "") if my_color == "white" else white.get("username", "")
    my_rating = white.get("rating") if my_color == "white" else black.get("rating")
    opp_rating = black.get("rating") if my_color == "white" else white.get("rating")
    my_result = _normalize_result(username, game)
    result_display = my_result.capitalize()
    opening = _opening_name(game.get("eco"))
    accuracies = game.get("accuracies")
    my_accuracy = None
    if accuracies and isinstance(accuracies, dict):
        my_accuracy = accuracies.get(my_color)
    end_time = game.get("end_time")
    date_str = time.strftime("%Y-%m-%d", time.localtime(end_time)) if end_time else "Unknown date"
    
    content_lines = [f"## {time_class} vs {opponent} ({my_result})"]
    content_lines.append(f"- Result: {result_display}")
    content_lines.append(f"- My rating: {my_rating} (opponent: {opp_rating})")
    content_lines.append(f"- Date: {date_str}")
    if opening:
        content_lines.append(f"- Opening: {opening}")
    if my_accuracy is not None:
        content_lines.append(f"- Accuracy: {my_accuracy}")
    pgn = game.get("pgn", "")
    if pgn:
        content_lines.append("### PGN")
        content_lines.append(pgn)
    
    return {
        "id": f"chesscom:{uuid}",
        "url": url,
        "title": f"{time_class} vs {opponent} ({my_result})",
        "content": "\n".join(content_lines),
    }


def chesscom_source(username: str | None = None, months_back: int = 12):
    username = username or os.environ.get("CHESSCOM_USERNAME")
    if not username:
        raise ValueError("Chess.com username required: pass username= or set CHESSCOM_USERNAME.")
    
    client = httpx.Client(headers={"User-Agent": USER_AGENT})
    
    # Get archives
    archives_url = f"{API_ROOT}/player/{username}/games/archives"
    archives_data = _get(client, archives_url)
    archives = archives_data.get("archives", [])
    
    # Take last `months_back` archives
    recent_archives = archives[-months_back:] if len(archives) > months_back else archives
    
    @dlt.resource(name="chesscom_documents", primary_key="id", write_disposition="replace")
    def chesscom_documents():
        for month_url in recent_archives:
            try:
                month_data = _get(client, month_url)
            except Exception as exc:
                print(f"[chesscom] month fetch failed {month_url}: {exc}")
                continue
            games = month_data.get("games", [])
            for game in games:
                try:
                    yield _build_row(username, game)
                except Exception as exc:
                    g_uuid = game.get("uuid", "unknown")
                    print(f"[chesscom] skipping game:{g_uuid}: {exc}")
    
    @dlt.source(name="chesscom")
    def _chesscom():
        return chesscom_documents
    
    source = _chesscom()
    setattr(source, DOCUMENT_SOURCE_ATTR, "chesscom")
    return source
