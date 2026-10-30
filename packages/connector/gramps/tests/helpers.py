"""Shared helpers: a client wired to the fake server, and one sync run over a dict state."""

from __future__ import annotations

from fake_gramps import BASE_URL, PASSWORD, USERNAME, FakeGramps

from cognee_community_connector_gramps import gramps as g


def client_for(fake: FakeGramps, sleeps: list | None = None) -> g.GrampsClient:
    waits = sleeps if sleeps is not None else []
    return g.GrampsClient(BASE_URL, USERNAME, PASSWORD, session=fake.session, sleep=waits.append)


def run(client: g.GrampsClient, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(g._iter_rows(client, state, stats, **kwargs))
    return rows, stats


def by_id(rows: list[dict]) -> dict[str, dict]:
    return {row["id"]: row for row in rows}


def person(handle: str) -> str:
    return f"gramps:person:{handle}"


def family(handle: str) -> str:
    return f"gramps:family:{handle}"


def place(handle: str) -> str:
    return f"gramps:place:{handle}"
