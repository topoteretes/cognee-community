"""Unit tests for the Wikidata connector.

The Wikibase API is served by ``FakeWikibase`` through ``httpx.MockTransport``
(no network), so the real HTTP, batching and retry code runs. Coverage:

  - time values at every precision, entity id normalization
  - the first run renders readable documents: mul-label fallback, statements
    with qualifiers and units, preferred-first order, external ids and
    deprecated statements left out, sitelinks, the row contract
  - expansion along allowlisted properties, bounded by depth and max_items
  - later runs re-fetch only items whose lastrevid moved
  - deleted, merged (redirected) and never-assigned ids
  - items leaving the selection, the empty-selection guard
  - SPARQL selection and its error handling
  - render / selection setting changes trigger a full re-render
  - batching to 50 ids, retries, a failed run keeping its state
  - the dlt resource wiring, and a real dlt merge that drops a tombstoned row
"""

import copy

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    PIPELINE_SCOPE_ATTR,
)
from fake_wikibase import (
    API_URL,
    SPARQL_URL,
    FakeWikibase,
    item_value,
    statement,
    string_value,
    time_value,
)

from cognee_community_connector_wikidata import (
    WikidataAuthError,
    WikidataScopeError,
    normalize_entity_id,
    wikidata_source,
)
from cognee_community_connector_wikidata.wikidata import (
    WikidataAPIError,
    _Settings,
    _WikibaseClient,
    format_time,
    sync_items,
)


def _settings(**overrides) -> _Settings:
    values = {
        "items": ("Q42",),
        "expand_properties": ("P800",),
        "expand_depth": 1,
        "sparql": None,
        "sparql_variable": "item",
        "max_items": 500,
        "language": "en",
        "include_external_ids": False,
        "sitelinks": ("enwiki",),
        "full_sync": False,
    }
    values.update(overrides)
    for key in ("items", "expand_properties", "sitelinks"):
        values[key] = tuple(values[key])
    return _Settings(**values)


def _sync(wiki: FakeWikibase, state: dict, **overrides):
    """Run one sync; return (rows by id, tombstoned ids, stats)."""
    client = _WikibaseClient(
        wiki.client(), API_URL, "test-agent/1.0", 5, sparql_url=SPARQL_URL, sleep=lambda _s: None
    )
    stats: dict = {}
    rows = list(sync_items(client, _settings(**overrides), state, stats))
    live = {row["id"]: row for row in rows if not row["_deleted"]}
    deleted = {row["id"] for row in rows if row["_deleted"]}
    return live, deleted, stats


@pytest.fixture
def wiki() -> FakeWikibase:
    wiki = FakeWikibase()
    for entity_id, label in {
        "P31": "instance of",
        "P50": "author",
        "P106": "occupation",
        "P214": "VIAF ID",
        "P27": "country of citizenship",
        "P569": "date of birth",
        "P580": "start time",
        "P800": "notable work",
        "P2048": "height",
        "Q5": "human",
        "Q145": "United Kingdom",
        "Q36180": "writer",
        "Q11573": "metre",
    }.items():
        wiki.create(entity_id, label)
    wiki.create(
        "Q42",
        "Douglas Adams",
        # Like the live item, the label exists only for all languages.
        label_language="mul",
        descriptions={"en": "English writer and humorist"},
        aliases={"en": ["Douglas Noel Adams"]},
        claims=[
            statement("P31", item_value("Q5")),
            statement("P800", item_value("Q25169")),
            statement("P800", item_value("Q3107329")),
            statement("P569", time_value("+1952-03-11T00:00:00Z"), "time"),
            statement(
                "P106",
                item_value("Q36180"),
                qualifiers={"P580": [time_value("+1979-00-00T00:00:00Z", precision=9)]},
            ),
            statement(
                "P2048",
                {
                    "type": "quantity",
                    "value": {"amount": "+1.96", "unit": "http://www.wikidata.org/entity/Q11573"},
                },
                "quantity",
            ),
            statement("P214", string_value("113230702"), "external-id"),
            statement("P27", item_value("Q5"), rank="deprecated"),
            statement("P27", item_value("Q145"), rank="preferred"),
        ],
        sitelinks={"enwiki": "Douglas Adams", "dewiki": "Douglas Adams"},
    )
    wiki.create(
        "Q25169",
        "The Hitchhiker's Guide to the Galaxy",
        claims=[statement("P50", item_value("Q42"))],
    )
    wiki.create("Q3107329", "The Hitchhiker's Guide to the Galaxy (radio series)")
    return wiki


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("time", "precision", "expected"),
    [
        ("+1952-03-11T00:00:00Z", 11, "1952-03-11"),
        ("+1952-03-00T00:00:00Z", 10, "1952-03"),
        ("+1979-00-00T00:00:00Z", 9, "1979"),
        # Day precision claimed but the day is unknown: never print "-00".
        ("+1979-05-00T00:00:00Z", 11, "1979-05"),
        ("+1960-00-00T00:00:00Z", 8, "1960s"),
        ("+1901-00-00T00:00:00Z", 7, "20th century"),
        ("+2001-00-00T00:00:00Z", 6, "3rd millennium"),
        ("-0044-03-15T00:00:00Z", 11, "0044-03-15 BCE"),
        ("-13798000000-00-00T00:00:00Z", 3, "about 13798000000 years ago"),
    ],
)
def test_format_time_respects_precision(time, precision, expected):
    assert format_time({"time": time, "precision": precision}) == expected


def test_format_time_marks_the_julian_calendar():
    value = time_value("+1564-04-26T00:00:00Z")["value"]
    value["calendarmodel"] = "http://www.wikidata.org/entity/Q1985786"
    assert format_time(value) == "1564-04-26 (Julian calendar)"


@pytest.mark.parametrize(
    "raw",
    [
        "Q42",
        "q42",
        " Q42 ",
        "http://www.wikidata.org/entity/Q42",
        "https://www.wikidata.org/wiki/Q42/",
    ],
)
def test_normalize_entity_id_accepts_ids_uris_and_urls(raw):
    assert normalize_entity_id(raw) == "Q42"


def test_normalize_entity_id_handles_properties_and_rejects_garbage():
    assert normalize_entity_id("https://www.wikidata.org/wiki/Property:P31") == "P31"
    for bad in ("Douglas Adams", "Q0", "L123", ""):
        with pytest.raises(ValueError):
            normalize_entity_id(bad)


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------
def test_initial_sync_renders_readable_documents(wiki):
    state: dict = {}
    live, deleted, stats = _sync(wiki, state)

    # The seed plus its notable works (one hop along P800).
    assert set(live) == {"Q42", "Q25169", "Q3107329"}
    assert deleted == set()
    assert stats["full_sync"] == 1 and stats["full_sync_initial"] == 1

    row = live["Q42"]
    assert row["title"] == "Douglas Adams"  # resolved through the mul fallback
    assert row["url"] == "https://www.wikidata.org/wiki/Q42"
    assert row["_deleted"] is False
    assert row[NODE_SET_COLUMN] == ["wikidata:www.wikidata.org"]

    content = row["content"]
    assert content.startswith("# Douglas Adams (Q42)\nEnglish writer and humorist\n")
    assert "Also known as: Douglas Noel Adams" in content
    assert "- instance of: human" in content
    assert "- notable work: The Hitchhiker's Guide to the Galaxy" in content
    assert "- date of birth: 1952-03-11" in content
    assert "- occupation: writer (start time: 1979)" in content
    assert "- height: 1.96 metre" in content
    assert "- country of citizenship: United Kingdom" in content
    # External ids and deprecated statements are left out by default.
    assert "VIAF" not in content and "113230702" not in content
    assert "country of citizenship: human" not in content
    # Only the configured sitelinks.
    assert "- enwiki: Douglas Adams (https://en.wikipedia.org/wiki/Douglas_Adams)" in content
    assert "dewiki" not in content


def test_preferred_statements_come_first():
    wiki = FakeWikibase()
    wiki.create("P1082", "population")
    wiki.create(
        "Q1",
        "Town",
        claims=[
            statement(
                "P1082", {"type": "quantity", "value": {"amount": "+100", "unit": "1"}}, "quantity"
            ),
            statement(
                "P1082",
                {"type": "quantity", "value": {"amount": "+250", "unit": "1"}},
                "quantity",
                rank="preferred",
            ),
        ],
    )
    live, _, _ = _sync(wiki, {}, items=["Q1"], expand_properties=[])
    assert "- population: 250\n- population: 100" in live["Q1"]["content"]


def test_unknown_and_no_value_statements_are_spelled_out():
    wiki = FakeWikibase()
    wiki.create("P22", "father")
    wiki.create("P40", "child")
    wiki.create(
        "Q1",
        "Someone",
        claims=[
            statement("P22", None, snaktype="somevalue"),
            statement("P40", None, snaktype="novalue"),
        ],
    )
    content = _sync(wiki, {}, items=["Q1"], expand_properties=[])[0]["Q1"]["content"]
    assert "- father: unknown value" in content
    assert "- child: no value" in content


def _text(text: str, language: str) -> dict:
    return {"type": "monolingualtext", "value": {"text": text, "language": language}}


def test_other_language_texts_are_dropped_when_one_matches():
    wiki = FakeWikibase()
    wiki.create("P1476", "title")
    wiki.create("P1705", "native label")
    wiki.create("P2096", "media legend")
    wiki.create("P18", "image")
    caption = {"P2096": [_text("A grave", "en"), _text("Ein Grab", "de")]}
    wiki.create(
        "Q1",
        "Thing",
        claims=[
            statement("P1476", _text("The Title", "en"), "monolingualtext"),
            statement("P1476", _text("Der Titel", "de"), "monolingualtext"),
            # No English value at all: every language is kept.
            statement("P1705", _text("Douze", "fr"), "monolingualtext"),
            statement("P1705", _text("Zwölf", "de"), "monolingualtext"),
            statement("P18", string_value("Grave.jpg"), "commonsMedia", qualifiers=caption),
        ],
    )
    content = _sync(wiki, {}, items=["Q1"], expand_properties=[])[0]["Q1"]["content"]
    assert "- title: The Title" in content and "Der Titel" not in content
    assert "- native label: Douze (fr)" in content and "- native label: Zwölf (de)" in content
    assert "- image: Grave.jpg (media legend: A grave)" in content


def test_external_ids_can_be_included(wiki):
    live, _, _ = _sync(wiki, {}, include_external_ids=True, expand_properties=[])
    assert "- VIAF ID: 113230702" in live["Q42"]["content"]


def test_labels_are_resolved_in_batches_and_never_twice(wiki):
    _sync(wiki, {})
    label_requests = [r for r in wiki.requests if r.get("props") == "labels"]
    # One request per batch of rendered items at most (here: the seed's level and
    # its notable works' level), each with language fallback for `mul` labels.
    assert 1 <= len(label_requests) <= 2
    assert all(r["languagefallback"] == "1" for r in label_requests)
    asked = [entity_id for r in label_requests for entity_id in r["ids"].split("|")]
    assert len(asked) == len(set(asked)), "the label cache spans the whole run"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def test_expansion_is_bounded_by_depth(wiki):
    # Q25169 points back to Q42 through P50, not P800: nothing new at depth 2.
    live, _, _ = _sync(wiki, {}, expand_properties=["P800", "P50"], expand_depth=0)
    assert set(live) == {"Q42"}
    live, _, _ = _sync(wiki, {}, expand_properties=["P800", "P50"], expand_depth=5)
    assert set(live) == {"Q42", "Q25169", "Q3107329"}


def test_max_items_caps_the_selection(wiki):
    live, _, stats = _sync(wiki, {}, max_items=2)
    assert set(live) == {"Q42", "Q25169"}
    assert stats["truncated"] == 1


def test_a_misspelled_or_unknown_seed_raises_instead_of_syncing_less(wiki):
    state: dict = {}
    with pytest.raises(WikidataScopeError, match="Q999999999"):
        _sync(wiki, state, items=["Q42", "Q999999999"], expand_properties=[])
    assert state == {}


def test_a_never_assigned_id_does_not_block_its_batch(wiki):
    # Wikidata fails the whole wbgetentities request for an id that was never
    # assigned; the client drops that id and retries the rest of the batch.
    client = _WikibaseClient(wiki.client(), API_URL, "ua", None, sleep=lambda _s: None)
    entities = client.get_entities(["Q42", "Q999999999"], {"props": "info"})
    assert "missing" in entities["Q999999999"]
    assert entities["Q42"]["id"] == "Q42"


def test_a_seed_deleted_after_it_was_synced_is_forgotten_not_rejected(wiki):
    state: dict = {}
    _sync(wiki, state, items=["Q42", "Q25169"], expand_properties=[])
    wiki.delete("Q25169")
    live, deleted, stats = _sync(wiki, state, items=["Q42", "Q25169"], expand_properties=[])
    assert live == {} and deleted == {"Q25169"}
    assert stats["failed"] == 0


def test_sparql_selects_items(wiki):
    wiki.sparql_results = ["Q25169", "Q3107329", "Q25169"]
    live, _, _ = _sync(
        wiki, {}, items=[], expand_properties=[], sparql="SELECT ?item WHERE {} LIMIT 10"
    )
    assert set(live) == {"Q25169", "Q3107329"}
    sparql = next(r for r in wiki.requests if r["_url"] == SPARQL_URL)
    assert sparql["query"] == "SELECT ?item WHERE {} LIMIT 10"
    assert sparql["_user_agent"] == "test-agent/1.0"


def test_a_sparql_timeout_raises(wiki):
    # The query service reports a timeout as a text body, not JSON results.
    wiki.sparql_raw = httpx.Response(200, text="java.util.concurrent.TimeoutException")
    with pytest.raises(WikidataAPIError, match="sparql_bad_response"):
        _sync(wiki, {}, items=[], expand_properties=[], sparql="SELECT ?item WHERE {}")


def test_more_than_fifty_ids_are_batched(wiki):
    for number in range(1, 121):
        wiki.create(f"Q{1_000_000 + number}", f"Item {number}")
    items = [f"Q{1_000_000 + number}" for number in range(1, 121)]
    live, _, _ = _sync(wiki, {}, items=items, expand_properties=[])
    assert len(live) == 120
    sizes = [len(r["ids"].split("|")) for r in wiki.requests if r.get("ids")]
    assert max(sizes) <= 50


# ---------------------------------------------------------------------------
# Later runs
# ---------------------------------------------------------------------------
def test_unchanged_run_costs_only_revision_checks(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.requests.clear()

    live, deleted, stats = _sync(wiki, state)
    assert live == {} and deleted == set()
    assert stats["full_sync"] == 0
    assert stats["items_unchanged"] == 3
    # Two levels of revision checks, no entity or label fetches.
    assert [r["props"] for r in wiki.requests] == ["info", "info"]


def test_an_edited_item_is_re_rendered_alone(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.edit("Q25169", descriptions={"en": "comedy science fiction series"})

    live, deleted, stats = _sync(wiki, state)
    assert set(live) == {"Q25169"}
    assert "comedy science fiction series" in live["Q25169"]["content"]
    assert deleted == set()
    assert stats["items_changed"] == 1 and stats["items_unchanged"] == 2


def test_deleted_item_is_tombstoned(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.delete("Q3107329")

    live, deleted, stats = _sync(wiki, state)
    assert live == {} and deleted == {"Q3107329"}
    assert stats["deleted"] == 1
    assert "Q3107329" not in state["items"]


def test_merged_item_is_replaced_by_its_target(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.create("Q777", "Hitchhiker's Guide (radio)")
    wiki.merge("Q3107329", "Q777")

    live, deleted, stats = _sync(wiki, state)
    # Q42 still links the old id; it now resolves to the merge target.
    assert set(live) == {"Q777"}
    assert deleted == {"Q3107329"}
    assert stats["redirected"] == 1


def test_item_leaving_the_selection_is_forgotten(wiki):
    state: dict = {}
    _sync(wiki, state)
    claims = [
        c
        for c in wiki.entities["Q42"].claims
        if c["mainsnak"].get("datavalue", {}).get("value", {})
        != {"entity-type": "item", "numeric-id": 3107329, "id": "Q3107329"}
    ]
    wiki.edit("Q42", claims=claims)

    live, deleted, _ = _sync(wiki, state)
    assert set(live) == {"Q42"}
    assert deleted == {"Q3107329"}


def test_empty_selection_never_mass_deletes(wiki):
    state: dict = {}
    wiki.sparql_results = ["Q25169"]
    _sync(wiki, state, items=[], expand_properties=[], sparql="SELECT ?item WHERE {}")
    wiki.sparql_results = []

    live, deleted, _ = _sync(
        wiki, state, items=[], expand_properties=[], sparql="SELECT ?item WHERE {}"
    )
    assert live == {} and deleted == set()
    assert set(state["items"]) == {"Q25169"}


def test_render_change_re_renders_everything(wiki):
    state: dict = {}
    _sync(wiki, state)
    live, _, stats = _sync(wiki, state, include_external_ids=True)
    assert stats["full_sync_render_changed"] == 1
    assert set(live) == {"Q42", "Q25169", "Q3107329"}


def test_full_sync_flag_re_renders_everything(wiki):
    state: dict = {}
    _sync(wiki, state)
    live, _, stats = _sync(wiki, state, full_sync=True)
    assert stats["full_sync_requested"] == 1
    assert len(live) == 3


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_requests_identify_the_client_and_send_maxlag(wiki):
    _sync(wiki, {})
    for request in wiki.requests:
        assert request["_user_agent"] == "test-agent/1.0"
        assert request["maxlag"] == "5"


@pytest.mark.parametrize(
    "failure", [("api", "maxlag"), ("http", 429), ("http", 503), ("network", None)]
)
def test_transient_failures_are_retried(wiki, failure):
    wiki.failures = [failure, failure]
    live, _, _ = _sync(wiki, {})
    assert set(live) == {"Q42", "Q25169", "Q3107329"}


def test_api_error_aborts_the_run_and_keeps_the_previous_state(wiki):
    state: dict = {}
    _sync(wiki, state)
    snapshot = copy.deepcopy(state)
    wiki.delete("Q3107329")
    wiki.failures = [("api", "internal_api_error_DBQueryError")]

    with pytest.raises(WikidataAPIError):
        _sync(wiki, state)
    assert state == snapshot

    _, deleted, _ = _sync(wiki, state)
    assert deleted == {"Q3107329"}


def test_retries_give_up_eventually(wiki):
    wiki.failures = [("http", 503)] * 10
    with pytest.raises(WikidataAPIError, match="http_503"):
        _sync(wiki, {})


# ---------------------------------------------------------------------------
# dlt resource
# ---------------------------------------------------------------------------
def test_resource_is_a_merge_document_source():
    resource = wikidata_source(["Q42"], service=httpx.Client())
    assert resource.name == "wikidata_www_wikidata_org"
    assert resource.write_disposition == "merge"
    assert resource.columns["_deleted"]["hard_delete"] is True
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "wikidata"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == resource.name
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "Select something"),
        ({"items": ["Douglas Adams"]}, "Not a Wikidata"),
        ({"items": ["Q42"], "expand_properties": ["Q5"]}, "property ids"),
        ({"items": ["Q42"], "max_items": 0}, "max_items"),
        ({"items": ["Q42"], "language": "English"}, "language"),
        ({"items": ["Q42"], "api_url": "https://www.wikidata.org/wiki/Q42"}, "api.php"),
        ({"items": ["Q42"], "api_url": ""}, "api.php"),
        ({"items": ["Q42"], "user_agent": " "}, "user_agent"),
        (
            {"sparql": "SELECT ?item {}", "api_url": "https://example.wikibase.cloud/w/api.php"},
            "sparql_url",
        ),
    ],
)
def test_factory_validates_its_arguments(kwargs, message, monkeypatch):
    # The factory reads no environment: a set variable must not fill a gap.
    monkeypatch.setenv("WIKIDATA_SPARQL_URL", SPARQL_URL)
    with pytest.raises(ValueError, match=message):
        wikidata_source(**kwargs)


def _pipeline(tmp_path, name="wikidata_test"):
    pytest.importorskip("duckdb")
    return dlt.pipeline(
        pipeline_name=name,
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.duckdb(str(tmp_path / "staging.duckdb")),
        dataset_name="wikidata",
    )


def _staged(pipeline, table):
    with pipeline.sql_client() as client:
        rows = client.execute_sql(f"SELECT id, title FROM {table} ORDER BY id")
    return dict(rows)


def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(wiki, tmp_path):
    pipeline = _pipeline(tmp_path)

    def run():
        source = wikidata_source(["Q42"], expand_properties=["P800"], service=wiki.client())
        pipeline.run(source)
        return source

    source = run()
    table = source.name
    assert _staged(pipeline, table) == {
        "Q25169": "The Hitchhiker's Guide to the Galaxy",
        "Q3107329": "The Hitchhiker's Guide to the Galaxy (radio series)",
        "Q42": "Douglas Adams",
    }
    assert source.cognee_sync_stats["full_sync"] == 1

    # The state persists in dlt's resource state: the next run is incremental.
    wiki.delete("Q3107329")
    wiki.edit("Q25169", labels={"en": "Hitchhiker's Guide"})
    source = run()
    assert source.cognee_sync_stats["full_sync"] == 0
    assert _staged(pipeline, table) == {"Q25169": "Hitchhiker's Guide", "Q42": "Douglas Adams"}


def test_one_resource_name_cannot_hold_two_wikibases(wiki, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(wikidata_source(["Q42"], service=wiki.client(), resource_name="items"))

    other = FakeWikibase()
    other_url = "https://example.wikibase.cloud/w/api.php"
    with pytest.raises(Exception, match="another Wikibase"):
        pipeline.run(
            wikidata_source(
                ["Q42"], api_url=other_url, service=other.client(), resource_name="items"
            )
        )


# ---------------------------------------------------------------------------
# Hardening (same review checklist as the other connectors)
# ---------------------------------------------------------------------------
def test_stats_are_plain_int_counters(wiki):
    _, _, stats = _sync(wiki, {})
    assert stats and all(type(value) is int for value in stats.values())


def test_a_long_retry_after_stops_the_run_cleanly(wiki):
    state: dict = {}
    _sync(wiki, state)
    snapshot = copy.deepcopy(state)
    wiki.edit("Q25169", descriptions={"en": "edited during an overload"})
    wiki.failures = [("http", (429, "3600"))]

    live, _, stats = _sync(wiki, state)  # no exception, no hour-long sleep
    assert live == {}
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1
    assert state == snapshot

    live, _, _ = _sync(wiki, state)
    assert set(live) == {"Q25169"}, "the next run picks the edit up"


def test_an_empty_selection_reports_failure_and_keeps_the_state(wiki):
    state: dict = {}
    wiki.sparql_results = ["Q25169"]
    _sync(wiki, state, items=[], expand_properties=[], sparql="SELECT ?item WHERE {}")
    snapshot = copy.deepcopy(state)
    wiki.sparql_results = []

    _, _, stats = _sync(wiki, state, items=[], expand_properties=[], sparql="SELECT ?item WHERE {}")
    assert stats["failed"] == 1 and stats["failed_empty_scope"] == 1
    # Only the empty-run counter is recorded.
    assert state.pop("empty_runs") == 1
    assert state == snapshot


def test_a_refused_request_counts_as_an_auth_failure(wiki):
    wiki.failures = [("http", (403, "0"))]
    stats: dict = {}
    client = _WikibaseClient(wiki.client(), API_URL, "ua", 5, sleep=lambda _s: None)
    with pytest.raises(WikidataAuthError):
        list(sync_items(client, _settings(), {}, stats))
    assert stats["failed"] == 1 and stats["failed_auth"] == 1


def test_a_non_json_answer_is_an_error_not_an_empty_result(wiki):
    wiki.failures = [("html", None)]
    with pytest.raises(WikidataAPIError, match="invalid_response"):
        _sync(wiki, {})


def test_the_injected_service_is_the_client_that_extracts(wiki, tmp_path):
    pipeline = _pipeline(tmp_path)
    source = wikidata_source(["Q42"], service=wiki.client())
    pipeline.run(source)
    assert _staged(pipeline, source.name) == {"Q42": "Douglas Adams"}
    assert wiki.requests, "every request went through the injected client"


def test_two_wikibases_with_their_own_resource_names_share_a_dataset(wiki, tmp_path):
    pipeline = _pipeline(tmp_path)
    other = FakeWikibase()
    other.create("Q1", "Local item")
    other_url = "https://example.wikibase.cloud/w/api.php"

    def run_both():
        pipeline.run(wikidata_source(["Q42"], service=wiki.client(), resource_name="wd"))
        pipeline.run(
            wikidata_source(["Q1"], api_url=other_url, service=other.client(), resource_name="wb")
        )

    run_both()
    other.delete("Q1")
    run_both()
    assert _staged(pipeline, "wd") == {"Q42": "Douglas Adams"}
    # The other Wikibase's only item was confirmed deleted, so it is forgotten
    # there, and the Wikidata source is untouched.
    assert _staged(pipeline, "wb") == {}


def test_deleting_the_only_selected_item_is_forgotten_not_guarded(wiki):
    state: dict = {}
    _sync(wiki, state, items=["Q3107329"], expand_properties=[])
    wiki.delete("Q3107329")
    live, deleted, stats = _sync(wiki, state, items=["Q3107329"], expand_properties=[])
    assert live == {} and deleted == {"Q3107329"}
    assert stats["failed"] == 0


# ---------------------------------------------------------------------------
# Review round 2 (same points as the MediaWiki connector)
# ---------------------------------------------------------------------------
def test_an_empty_query_result_is_forgotten_on_the_second_empty_run(wiki):
    state: dict = {}
    query = "SELECT ?item WHERE {}"
    wiki.sparql_results = ["Q25169", "Q3107329"]
    _sync(wiki, state, items=[], expand_properties=[], sparql=query)
    wiki.sparql_results = []

    _, deleted, stats = _sync(wiki, state, items=[], expand_properties=[], sparql=query)
    assert deleted == set() and stats["failed_empty_scope"] == 1  # could be an outage
    _, deleted, stats = _sync(wiki, state, items=[], expand_properties=[], sparql=query)
    assert deleted == {"Q25169", "Q3107329"}, "a second empty run is believed"
    assert stats["failed"] == 0 and state["items"] == {}


def test_switching_to_an_empty_query_forgets_the_old_items(wiki):
    state: dict = {}
    wiki.sparql_results = ["Q25169"]
    _sync(wiki, state, items=[], expand_properties=[], sparql="SELECT ?item WHERE { a }")
    wiki.sparql_results = []

    _, deleted, stats = _sync(
        wiki, state, items=[], expand_properties=[], sparql="SELECT ?item WHERE { b }"
    )
    assert stats["full_sync_selection_changed"] == 1
    assert deleted == {"Q25169"} and stats["failed"] == 0


def test_a_rate_limited_run_makes_progress_across_runs(monkeypatch):
    import cognee_community_connector_wikidata.wikidata as module

    monkeypatch.setattr(module, "_BATCH_SIZE", 2)  # small batches, so a stop is mid-run
    wiki = FakeWikibase()
    seeds = [f"Q{100 + number}" for number in range(8)]
    for number, entity_id in enumerate(seeds):
        wiki.create(entity_id, f"Item {number}")
    # The revision check and two batches go through, then the API asks for an hour.
    wiki.failures = [("pass", None)] * 3 + [("http", (429, "3600"))]

    state: dict = {}
    _, _, first = _sync(wiki, state, items=seeds, expand_properties=[])
    assert first["failed_rate_limit"] == 1 and 0 < first["items_changed"] < 8

    _, _, second = _sync(wiki, state, items=seeds, expand_properties=[])
    assert second["failed"] == 0
    assert first["items_changed"] + second["items_changed"] == 8, "nothing fetched twice"


@pytest.mark.parametrize(
    ("overrides", "failures", "reason", "error"),
    [
        (
            {"items": ["Q42", "Q999999999"], "expand_properties": []},
            [],
            "scope",
            WikidataScopeError,
        ),
        ({}, [("api", "internal_api_error_DBQueryError")], "api", WikidataAPIError),
    ],
)
def test_every_failure_sets_failed(wiki, overrides, failures, reason, error):
    wiki.failures = list(failures)
    stats: dict = {}
    client = _WikibaseClient(wiki.client(), API_URL, "ua", 5, sleep=lambda _s: None)
    with pytest.raises(error):
        list(sync_items(client, _settings(**overrides), {}, stats))
    assert stats["failed"] == 1 and stats[f"failed_{reason}"] == 1
