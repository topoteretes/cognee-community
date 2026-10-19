"""An in-memory Wikibase (Wikidata) API served through ``httpx.MockTransport``.

It implements the slice the connector uses: ``wbgetentities`` (info, labels,
descriptions, aliases, claims, sitelinks; ``languagefallback``; ``sitefilter``)
and a SPARQL endpoint. Response shapes follow the live Wikidata API as observed
in October 2026, including its quirks:

* a deleted entity comes back as ``{"id": ..., "missing": ""}``;
* a merged entity is keyed by the requested id, carries
  ``"redirects": {"from", "to"}`` and the target's data;
* an id that was never assigned fails the whole request with
  ``no-such-entity`` (``error.id`` names it);
* many labels exist only under ``mul`` and are returned for another language
  only with ``languagefallback`` (as ``{"language": "mul", "for-language"}``).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import httpx

API_URL = "https://www.wikidata.org/w/api.php"
SPARQL_URL = "https://query.wikidata.org/sparql"
ENTITY_URI = "http://www.wikidata.org/entity/"


def item_value(entity_id: str) -> dict:
    return {
        "type": "wikibase-entityid",
        "value": {"entity-type": "item", "numeric-id": int(entity_id[1:]), "id": entity_id},
    }


def time_value(time: str, precision: int = 11) -> dict:
    return {
        "type": "time",
        "value": {
            "time": time,
            "timezone": 0,
            "before": 0,
            "after": 0,
            "precision": precision,
            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
        },
    }


def string_value(text: str) -> dict:
    return {"type": "string", "value": text}


def statement(
    property_id: str,
    datavalue: dict | None,
    datatype: str = "wikibase-item",
    *,
    rank: str = "normal",
    qualifiers: dict[str, list[dict]] | None = None,
    snaktype: str = "value",
) -> dict:
    mainsnak: dict = {"snaktype": snaktype, "property": property_id, "datatype": datatype}
    if datavalue is not None:
        mainsnak["datavalue"] = datavalue
    out = {"mainsnak": mainsnak, "type": "statement", "rank": rank}
    if qualifiers:
        out["qualifiers"] = {
            pid: [{"snaktype": "value", "property": pid, "datavalue": v} for v in values]
            for pid, values in qualifiers.items()
        }
        out["qualifiers-order"] = list(qualifiers)
    return out


@dataclass
class Entity:
    id: str
    labels: dict[str, str]
    descriptions: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    claims: list[dict] = field(default_factory=list)
    sitelinks: dict[str, str] = field(default_factory=dict)
    lastrevid: int = 0


class FakeWikibase:
    def __init__(self):
        self.entities: dict[str, Entity] = {}
        self.redirects: dict[str, str] = {}
        self.deleted: set[str] = set()
        self.next_revid = 1000
        self.requests: list[dict] = []
        self.sparql_results: list[str] = []
        self.sparql_raw: httpx.Response | None = None
        # Programmable failures: popped one per request, e.g. [("http", 429)].
        self.failures: list[tuple[str, object]] = []

    # -- editing helpers -------------------------------------------------------
    def create(self, entity_id: str, label: str, *, label_language="en", **kwargs) -> Entity:
        self.next_revid += 1
        entity = Entity(entity_id, {label_language: label}, lastrevid=self.next_revid, **kwargs)
        self.entities[entity_id] = entity
        self.deleted.discard(entity_id)
        return entity

    def edit(self, entity_id: str, **changes) -> None:
        entity = self.entities[entity_id]
        for name, value in changes.items():
            setattr(entity, name, value)
        self.next_revid += 1
        entity.lastrevid = self.next_revid

    def delete(self, entity_id: str) -> None:
        self.entities.pop(entity_id)
        self.deleted.add(entity_id)

    def merge(self, source_id: str, target_id: str) -> None:
        """Merge ``source_id`` into ``target_id``: the source becomes a redirect."""
        self.entities.pop(source_id)
        self.redirects[source_id] = target_id
        self.edit(target_id)

    # -- the API --------------------------------------------------------------
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        params["_url"] = str(request.url).split("?", 1)[0]
        params["_user_agent"] = request.headers.get("user-agent", "")
        self.requests.append(params)

        if self.failures:
            kind, value = self.failures.pop(0)
            if kind == "http":
                status, retry_after = value if isinstance(value, tuple) else (value, "0")
                return httpx.Response(status, headers={"Retry-After": retry_after})
            if kind == "html":
                return httpx.Response(200, text="<html>Maintenance</html>")
            if kind == "api":
                return httpx.Response(
                    200, headers={"Retry-After": "0"}, json={"error": {"code": value, "info": ""}}
                )
            if kind == "network":
                raise httpx.ConnectError("boom", request=request)

        if params["_url"] == SPARQL_URL:
            if self.sparql_raw is not None:
                return self.sparql_raw
            bindings = [
                {"item": {"type": "uri", "value": ENTITY_URI + entity_id}}
                for entity_id in self.sparql_results
            ]
            return httpx.Response(
                200, json={"head": {"vars": ["item"]}, "results": {"bindings": bindings}}
            )

        if params.get("action") != "wbgetentities":
            return httpx.Response(200, json={"error": {"code": "badvalue", "info": "action"}})
        return httpx.Response(200, json=self._wbgetentities(params))

    def _wbgetentities(self, params: dict) -> dict:
        ids = [i for i in params.get("ids", "").split("|") if i]
        if len(ids) > 50:
            return {"error": {"code": "toomanyvalues", "info": "Too many values supplied"}}
        for entity_id in ids:
            known = entity_id in self.entities or entity_id in self.redirects
            if not known and entity_id not in self.deleted:
                return {
                    "error": {
                        "code": "no-such-entity",
                        "info": f'Could not find an entity with the ID "{entity_id}".',
                        "id": entity_id,
                    }
                }
        props = set((params.get("props") or "info").split("|"))
        language = params.get("languages", "en")
        fallback = params.get("languagefallback") == "1"
        sites = set(params["sitefilter"].split("|")) if params.get("sitefilter") else None
        entities = {}
        for entity_id in ids:
            target_id = self.redirects.get(entity_id, entity_id)
            entity = self.entities.get(target_id)
            if entity is None:
                entities[entity_id] = {"id": entity_id, "missing": ""}
                continue
            out: dict = {"type": "item", "id": entity.id}
            if "info" in props:
                out.update(pageid=int(entity.id[1:]), ns=0, title=entity.id)
                out.update(lastrevid=entity.lastrevid, modified="2026-10-09T00:00:00Z")
            if target_id != entity_id:
                out["redirects"] = {"from": entity_id, "to": target_id}
            if "labels" in props:
                out["labels"] = _terms(entity.labels, language, fallback)
            if "descriptions" in props:
                out["descriptions"] = _terms(entity.descriptions, language, fallback)
            if "aliases" in props:
                values = entity.aliases.get(language) or []
                out["aliases"] = (
                    {language: [{"language": language, "value": v} for v in values]}
                    if values
                    else {}
                )
            if "claims" in props:
                claims: dict[str, list] = {}
                for claim in entity.claims:
                    claims.setdefault(claim["mainsnak"]["property"], []).append(
                        copy.deepcopy(claim)
                    )
                out["claims"] = claims
            if "sitelinks/urls" in props:
                out["sitelinks"] = {
                    site: {
                        "site": site,
                        "title": title,
                        "badges": [],
                        "url": f"https://{site[:-4]}.wikipedia.org/wiki/{title.replace(' ', '_')}",
                    }
                    for site, title in entity.sitelinks.items()
                    if sites is None or site in sites
                }
            entities[entity_id] = out
        return {"entities": entities, "success": 1}


def _terms(values: dict[str, str], language: str, fallback: bool) -> dict:
    if language in values:
        return {language: {"language": language, "value": values[language]}}
    if fallback and "mul" in values:
        return {language: {"language": "mul", "value": values["mul"], "for-language": language}}
    return {}
