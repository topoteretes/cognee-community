"""AniList dlt source (official GraphQL public API, no auth needed)."""
from __future__ import annotations
import os, time
from typing import Any
import dlt, httpx
try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except Exception:
    DOCUMENT_SOURCE_ATTR = "dlt_source"

API_ROOT = "https://graphql.anilist.co"


def _post(client: httpx.Client, query: str, variables: dict | None = None) -> dict[str, Any]:
    time.sleep(0.7)
    payload = {"query": query, "variables": variables or {}}
    for attempt in range(4):
        try:
            resp = client.post(API_ROOT, json=payload, timeout=30.0, headers={"Content-Type":"application/json","User-Agent":"cognee-anilist-connector"})
        except httpx.HTTPError as exc:
            if attempt == 3: raise exc
            time.sleep(0.7); continue
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            wait = float(ra) if ra and ra.isdigit() else 2.0
            time.sleep(wait); time.sleep(0.7)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("GraphQL request failed")


def _build_row(entry: dict[str, Any]) -> dict[str, Any]:
    media = entry.get("media") or {}
    mal_id = media.get("id")
    title = ""
    if isinstance(media.get("title"), dict):
        title = media["title"].get("english") or media["title"].get("romaji") or ""
    url = media.get("siteUrl", f"https://anilist.co/anime/{mal_id}")
    description = (media.get("description") or "").strip()
    status_raw = entry.get("status", "")
    status_map = {"CURRENT":"watching","PLANNING":"plan to watch","COMPLETED":"completed","DROPPED":"dropped","PAUSED":"paused","REPEATING":"rewatching"}
    status_label = status_map.get(status_raw, status_raw or "unknown")
    score = entry.get("score") or 0
    progress = entry.get("progress") or 0
    notes = (entry.get("notes") or "").strip()
    genres = ", ".join(str(g) for g in media.get("genres") or [])
    studios = ", ".join(str(s.get("name","")) for s in (media.get("studios",{}).get("nodes") or []) if isinstance(s,dict))
    avg_score = media.get("averageScore")
    content_lines = [f"## {title}"]
    content_lines.append("**My list**")
    content_lines.append(f"- Status: {status_label.lower()}")
    content_lines.append(f"- My score: {score}/10")
    content_lines.append(f"- Episodes watched: {progress}")
    if notes:
        content_lines.append(f"- Notes: {notes}")
    if description:
        content_lines.append("### Synopsis")
        content_lines.append(description)
    meta = []
    if genres: meta.append(f"Genres: {genres}")
    if studios: meta.append(f"Studios: {studios}")
    if avg_score is not None: meta.append(f"AniList avg: {avg_score}")
    if meta:
        content_lines.append("---")
        content_lines.append(" ".join(meta))
    return {"id":f"anilist:{mal_id}","url":url,"title":title,"content":"\n".join(content_lines)}


def anilist_source(username: str | None = None):
    username = username or os.environ.get("ANILIST_USERNAME")
    if not username:
        raise ValueError("AniList username required: pass username= or set ANILIST_USERNAME.")
    client = httpx.Client(base_url=API_ROOT, headers={"Content-Type":"application/json","User-Agent":"cognee-anilist-connector"})
    query = "query ($userName: String, $chunk: Int) { MediaListCollection(userName: $userName, type: ANIME, chunk: $chunk, perChunk: 50) { lists { name entries { status score progress updatedAt notes media { id title { romaji english } siteUrl description(asHtml: false) genres studios { nodes { name } } averageScore } } } hasNextChunk } }"
    
    @dlt.resource(name="anilist_documents", primary_key="id", write_disposition="replace")
    def anilist_documents():
        chunk = 1
        while True:
            try:
                result = _post(client, query, {"userName": username, "chunk": chunk})
            except Exception as exc:
                print(f"[anilist] chunk {chunk} failed: {exc}")
                break
            collection = result.get("data", {}).get("MediaListCollection", {})
            lists = collection.get("lists", [])
            for lst in lists:
                for entry in lst.get("entries") or []:
                    try:
                        yield _build_row(entry)
                    except Exception as exc:
                        media_id = (entry.get("media") or {}).get("id")
                        print(f"[anilist] skipping media:{media_id}: {exc}")
            if not collection.get("hasNextChunk", False):
                break
            chunk += 1
    
    @dlt.source(name="anilist")
    def _anilist():
        return anilist_documents
    
    source = _anilist()
    setattr(source, DOCUMENT_SOURCE_ATTR, "anilist")
    return source
