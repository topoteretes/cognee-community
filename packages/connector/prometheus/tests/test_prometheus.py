"""Unit tests for the Prometheus dlt connector.

Everything runs offline. A fake ``requests`` transport adapter stands in for a
Prometheus server, so the real ``rest_api`` source and auth run against
responses recorded from a local Prometheus 3.15.0 (``tests/fixtures``, rule
file paths shortened). Two layers:

* DB-free tests for the row builders (definition fields only, stable ids).
* dlt-pipeline tests into a temp sqlite destination covering the acceptance
  criteria: an unchanged server gives identical rows, an edited rule changes one
  row, a removed rule removes its row, and a failed or malformed read aborts
  instead of emptying staging.
"""

import base64
import copy
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.pipeline.exceptions import PipelineStepFailed
from requests.adapters import BaseAdapter

from cognee_community_connector_prometheus.prometheus import (
    PROMETHEUS_SOURCE_NAME,
    _default_resource_name,
    _default_session,
    _duration,
    _lines,
    _metric_rows,
    _rule_rows,
    prometheus_source,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "http://prometheus.test:9090"
# BASE_URL's resource name: prometheus_prometheus_test_9090_<hash of the address>.
NAME = _default_resource_name(urlsplit(BASE_URL))
RULES = f"{NAME}_rules"
METRICS = f"{NAME}_metrics"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _rules(body):
    """Rows the builder makes from one rules response, keyed by id."""
    return {row["id"]: row for row in _rule_rows(body["data"]["groups"], Counter())}


def _find(body, name, severity=None):
    """The rule object called ``name`` (and with this severity label, if given)."""
    for group in body["data"]["groups"]:
        for rule in group["rules"]:
            if rule["name"] == name and (
                severity is None or rule["labels"].get("severity") == severity
            ):
                return group, rule
    raise KeyError(name)


# ---------------------------------------------------------------------------
# Fake server
# ---------------------------------------------------------------------------


class FakePrometheus(BaseAdapter):
    """In-memory stand-in for a Prometheus server, mounted on a real requests.Session.

    It serves recorded bodies and applies the ``type`` and ``rule_group[]``
    filters the way the real server does. ``exclude_alerts`` is ignored, as an
    older server would, so the row builder has to drop the active alerts itself.
    """

    def __init__(self, rules=None, metadata=None):
        super().__init__()
        self.rules = rules if rules is not None else _fixture("rules_first_read.json")
        self.metadata = metadata if metadata is not None else _fixture("metadata.json")
        self.calls = []  # (path, params, headers)
        self.fail = None  # callable(path) -> (status, body bytes) or None
        self.ignore_filters = False  # like an older server that ignores type/rule_group[]
        self.retry_after = "0"  # Retry-After sent with every failure
        self.timeouts = []  # the timeout each request was sent with

    def session(self):
        session = requests.Session()
        session.mount("http://", self)
        return session

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        params = parse_qs(url.query)
        self.calls.append((url.path, params, dict(request.headers)))
        self.timeouts.append(kwargs.get("timeout"))

        failure = self.fail(url.path) if self.fail else None
        if isinstance(failure, Exception):
            if isinstance(failure, requests.RequestException) and failure.request is None:
                failure.request = request  # as requests' own adapter sets it
            raise failure  # e.g. a dropped connection
        if failure:
            response = self._response(request, *failure)
            response.headers["Retry-After"] = self.retry_after
            return response
        if url.path.endswith("/api/v1/rules"):
            body = self.rules if self.ignore_filters else self._filtered(params)
            return self._response(request, 200, json.dumps(body).encode())
        if url.path.endswith("/api/v1/metadata"):
            return self._response(request, 200, json.dumps(self.metadata).encode())
        return self._response(request, 404, b"404 page not found")

    def _filtered(self, params):
        body = copy.deepcopy(self.rules)
        kind = {"alert": "alerting", "record": "recording"}.get(params.get("type", [None])[0])
        groups = set(params.get("rule_group[]", []))
        kept = []
        for group in body["data"]["groups"]:
            if groups and group["name"] not in groups:
                continue
            group["rules"] = [rule for rule in group["rules"] if not kind or rule["type"] == kind]
            if group["rules"] or not kind:
                kept.append(group)
        body["data"]["groups"] = kept
        return body

    @staticmethod
    def _response(request, status, raw):
        response = requests.Response()
        response.status_code = status
        response.request = request
        response.url = request.url
        response.headers["Content-Type"] = "application/json"
        response._content = raw
        return response

    def close(self):
        pass

    def rule_calls(self):
        return [call for call in self.calls if call[0].endswith("/api/v1/rules")]


# ---------------------------------------------------------------------------
# Row builders (DB-free)
# ---------------------------------------------------------------------------


def test_rule_row_keeps_the_definition():
    body = _fixture("rules_second_read.json")
    rows = list(_rule_rows(body["data"]["groups"], Counter()))
    row = next(r for r in rows if "Labels: severity=page, team=payments" in r["content"])

    assert set(row) == {"id", "title", "content"}
    assert row["title"] == (
        "Prometheus alerting rule CheckoutHighErrorRate in group checkout-availability"
    )
    assert row["content"].splitlines() == [
        "Type: alerting rule",
        "Name: CheckoutHighErrorRate",
        "Group: checkout-availability",
        "File: /etc/prometheus/rules/app.yml",
        "Evaluation interval: 30s",
        'Expression: sum(rate(prometheus_http_requests_total{code=~"5.."}[5m]))'
        " / sum(rate(prometheus_http_requests_total[5m])) > 0.05",
        "For: 10m",
        "Labels: severity=page, team=payments",
        "Summary: Checkout is returning 5xx for more than 5% of requests",
        "Description: The checkout API error ratio has been above 5% for 10 minutes."
        " Customers cannot pay.",
        "Runbook: https://runbooks.example.com/checkout/high-error-rate",
    ]


def test_recording_rule_row():
    rows = _rules(_fixture("rules_first_read.json"))
    (row,) = [r for r in rows.values() if r["content"].startswith("Type: recording rule")]
    assert row["title"] == (
        "Prometheus recording rule job:prometheus_http_requests:rate5m in group checkout-recording"
    )
    assert "Expression: sum by (job) (rate(prometheus_http_requests_total[5m]))" in row["content"]


def test_runtime_state_never_reaches_a_row():
    # Two real reads of an unchanged server, 12 seconds apart: lastEvaluation and
    # evaluationTime differ, and HeartbeatAlwaysFiring carries an active alert.
    first, second = _fixture("rules_first_read.json"), _fixture("rules_second_read.json")
    assert first != second
    assert _rules(first) == _rules(second)

    _, rule = _find(second, "HeartbeatAlwaysFiring")
    assert rule["state"] == "firing" and rule["alerts"]
    edited = copy.deepcopy(second)
    for group in edited["data"]["groups"]:
        group["evaluationTime"] = 9.9
        for item in group["rules"]:
            item.update(state="pending", health="err", lastError="boom", alerts=[{"x": 1}])
    assert _rules(edited) == _rules(second)
    text = json.dumps(list(_rules(second).values()))
    # "firing" itself is in one rule's summary, so look for the runtime values.
    for runtime in ("inactive", '"firing"', "activeAt", "lastEvaluation", "evaluationTime"):
        assert runtime not in text


def test_same_name_with_other_labels_is_another_rule():
    rows = _rules(_fixture("rules_first_read.json"))
    titles = [row["title"] for row in rows.values()]
    assert (
        titles.count(
            "Prometheus alerting rule CheckoutHighErrorRate in group checkout-availability"
        )
        == 2
    )
    assert len(rows) == 5


def test_identical_rules_get_a_counter_instead_of_colliding():
    body = _fixture("rules_first_read.json")
    group, rule = _find(body, "HeartbeatAlwaysFiring")
    group["rules"].append(copy.deepcopy(rule))

    rows = _rules(body)
    assert len(rows) == 6
    # Same input order, same ids.
    assert set(_rules(copy.deepcopy(body))) == set(rows)


def test_editing_a_rule_keeps_its_id_and_changes_its_text():
    before = _fixture("rules_first_read.json")
    after = copy.deepcopy(before)
    _, rule = _find(after, "ScrapeTargetDown")
    rule["query"] = "up{job='node'} == 0"

    old, new = _rules(before), _rules(after)
    assert set(old) == set(new)
    changed = [rule_id for rule_id in old if old[rule_id] != new[rule_id]]
    assert len(changed) == 1
    assert "Expression: up{job='node'} == 0" in new[changed[0]]["content"]


def test_relabelling_a_rule_makes_it_a_new_rule():
    before = _fixture("rules_first_read.json")
    after = copy.deepcopy(before)
    _, rule = _find(after, "ScrapeTargetDown")
    rule["labels"]["severity"] = "ticket"
    assert len(set(_rules(before)) ^ set(_rules(after))) == 2


def test_other_annotations_follow_in_key_order():
    body = _fixture("rules_first_read.json")
    _, rule = _find(body, "HeartbeatAlwaysFiring")
    rule["annotations"].update(zeta="last", dashboard="https://grafana.example.com/d/x")
    (row,) = [r for r in _rules(body).values() if "HeartbeatAlwaysFiring" in r["title"]]
    assert row["content"].splitlines()[-3:] == [
        "Summary: Always firing, proves the pipeline is alive",
        "Annotation dashboard: https://grafana.example.com/d/x",
        "Annotation zeta: last",
    ]


def test_group_without_a_rules_list_aborts():
    with pytest.raises(ValueError, match="without a 'rules' list"):
        list(_rule_rows([{"name": "broken", "file": "x.yml"}], Counter()))


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (600, "10m"),
        (5400, "1h30m"),
        (90061, "1d1h1m1s"),
        (30, "30s"),
        (1.5, "1s500ms"),
        (0.25, "250ms"),
        # A real server returned this for `for: 12d500ms`; "%g" used to print 1.0368e+06s.
        (1036800.5, "12d500ms"),
    ],
)
def test_duration_reads_like_a_rule_file(seconds, text):
    assert _duration(seconds) == text


@pytest.mark.parametrize("seconds", [0, None, "10m", True, -5])
def test_duration_skips_empty_and_odd_values(seconds):
    assert _duration(seconds) is None


def test_metric_rows_from_metadata():
    rows = {row["id"]: row for row in _metric_rows(_fixture("metadata.json")["data"])}
    assert set(rows) == {
        "prometheus_http_requests_total",
        "prometheus_rule_evaluation_failures_total",
    }
    row = rows["prometheus_http_requests_total"]
    assert row["title"] == "Prometheus metric prometheus_http_requests_total"
    assert row["content"].startswith("Type: counter\nHelp: ")


def test_metric_with_several_entries_keeps_each_once_in_order():
    entries = [
        {"type": "gauge", "unit": "", "help": "b"},
        {"type": "gauge", "unit": "", "help": "a"},
        {"type": "gauge", "unit": "", "help": "b"},
    ]
    (row,) = _metric_rows({"m": entries})
    assert row["content"] == "Type: gauge\nHelp: a\nType: gauge\nHelp: b"


# ---------------------------------------------------------------------------
# Source construction
# ---------------------------------------------------------------------------


def test_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = prometheus_source(base_url=BASE_URL)
    assert PROMETHEUS_SOURCE_NAME == "prometheus"
    assert document_source_tag(source) == "prometheus"


@pytest.mark.parametrize("suffix", ["/prom?tenant=a", "/prom#rules", "/prom?", "/prom#"])
def test_source_rejects_a_url_with_a_query(suffix):
    with pytest.raises(ValueError, match="query string"):
        prometheus_source(base_url=f"{BASE_URL}{suffix}")


def test_source_rejects_credentials_in_the_url():
    # They would end up in dlt's logs and in error messages.
    with pytest.raises(ValueError, match="not in the URL"):
        prometheus_source(base_url="http://ops:s3cret@prometheus.test:9090")


@pytest.mark.parametrize("path", ["/api/v1", "/prom/api/v1/"])
def test_source_rejects_the_api_path_in_the_url(path):
    with pytest.raises(ValueError, match="without /api/v1"):
        prometheus_source(base_url=f"{BASE_URL}{path}")


def test_source_keeps_its_state_per_dataset():
    # The empty-snapshot flags live in dlt state; without a scope every unscoped
    # dlt source in cognee would share one pipeline state.
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    assert NAME.startswith("prometheus_prometheus_test_9090_")
    assert getattr(prometheus_source(base_url=BASE_URL), PIPELINE_SCOPE_ATTR) == (NAME)


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("http://localhost:9090", "prometheus_localhost_9090"),
        ("https://prom.example.com/prom/", "prometheus_prom_example_com_prom"),
        ("http://10.0.0.2:9090", "prometheus_10_0_0_2_9090"),
    ],
)
def test_each_server_gets_its_own_tables(url, name):
    (table,) = prometheus_source(base_url=url).resources.selected
    assert table.startswith(f"{name}_") and table.endswith("_rules")


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("http://prom-eu.example.com:9090", "http://prom.eu.example.com:9090"),
        ("http://prom.test:9090/prom/eu", "http://prom.test:9090/prom-eu"),
        ("http://prom.test:9090/Prom", "http://prom.test:9090/prom"),
    ],
)
def test_addresses_that_read_alike_get_different_tables(first, second):
    # Without the hash, both would fold to one name and share tables and state,
    # and each sync would forget the other server's rules.
    assert set(prometheus_source(base_url=first).resources.selected) != set(
        prometheus_source(base_url=second).resources.selected
    )


def test_a_long_address_gets_a_short_stable_name():
    url = "http://" + "very-long-prometheus-host-name." * 3 + "example.com:9090"
    first = next(iter(prometheus_source(base_url=url).resources.selected))
    assert first == next(iter(prometheus_source(base_url=url).resources.selected))
    assert len(first) <= 48 + len("_rules")
    assert "__" not in first, "a cut at an underscore must not leave two in a row"


@pytest.mark.parametrize("cut_at", range(30, 50))
def test_a_cut_long_name_never_has_two_underscores(cut_at):
    # Wherever the cut falls, the name must still pass the resource-name check.
    host = ("a" * (cut_at - len("prometheus_") - 1)) + "-b" * 20
    prometheus_source(base_url=f"http://{host}.example.com:9090")


def test_resource_name_can_be_set_and_must_fit_a_table_name():
    named = prometheus_source(base_url=BASE_URL, resource_name="prom_main")
    assert set(named.resources.selected) == {"prom_main_rules"}
    for bad in ("Prom-Main", "prom__main", "prom_", "1prom"):
        with pytest.raises(ValueError, match="resource_name"):
            prometheus_source(base_url=BASE_URL, resource_name=bad)


def test_an_older_cognee_is_refused(monkeypatch):
    # Without table-scoped cleanup, a sync of one table would forget the other.
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match=r"cognee>=1\.6\.1"):
        prometheus_source(base_url=BASE_URL)


def test_a_cognee_without_the_scope_attribute_is_refused(monkeypatch):
    # A dev build can carry the version number but not PIPELINE_SCOPE_ATTR yet.
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.delattr(dlt_utils, "PIPELINE_SCOPE_ATTR")
    with pytest.raises(RuntimeError, match=r"cognee>=1\.6\.1"):
        prometheus_source(base_url=BASE_URL)


def test_a_rule_that_is_not_an_object_aborts():
    groups = [{"name": "g", "file": "f", "rules": ["not a rule"]}]
    with pytest.raises(ValueError, match="not an object"):
        list(_rule_rows(groups, Counter()))


def _guard_groups_harness(monkeypatch, state):
    from cognee_community_connector_prometheus import prometheus

    warnings = []
    monkeypatch.setattr(prometheus.dlt.current, "source_state", lambda: state)
    monkeypatch.setattr(prometheus.logger, "warning", lambda *args: warnings.append(args))
    return prometheus, warnings


def test_a_skipped_group_keeps_the_time_of_the_run_that_accepted_it(monkeypatch):
    state = {}
    prometheus, warnings = _guard_groups_harness(monkeypatch, state)
    on_server = [{"name": "node-health", "rules": []}]
    wanted = ["node-health", "gone-a", "gone-b"]

    # gone-a is accepted on 1 October, gone-b on 27 October.
    monkeypatch.setattr(prometheus, "_now", lambda: "2026-10-01 09:00 UTC")
    stats = {}
    prometheus._guard_missing_groups(on_server, None, wanted[:2], True, stats)
    assert stats["skipped_groups"] == ["gone-a"]
    monkeypatch.setattr(prometheus, "_now", lambda: "2026-10-27 15:00 UTC")
    prometheus._guard_missing_groups(on_server, None, wanted, True, {})

    # A later run without the flag warns once per accepting run, with its time.
    warnings.clear()
    stats = {}
    prometheus._guard_missing_groups(on_server, None, wanted, False, stats)
    assert stats["skipped_groups"] == ["gone-a", "gone-b"]
    assert sorted(args[1:] for args in warnings) == [
        (1, "2026-10-01 09:00 UTC"),
        (1, "2026-10-27 15:00 UTC"),
    ]
    # Group names are the caller's own labels: none reaches a log message.
    assert not any(name in str(args) for args in warnings for name in wanted)

    # Back on the server, a group drops out; missing again, it stops the run again.
    back = [*on_server, {"name": "gone-a", "rules": []}]
    prometheus._guard_missing_groups(back, None, wanted[:2], False, {})
    assert state["forgotten_groups"] == {}
    with pytest.raises(ValueError, match="1 of the 2 rule groups"):
        prometheus._guard_missing_groups(on_server, None, wanted[:2], False, {})


def test_skipped_groups_saved_by_an_earlier_commit_are_read(monkeypatch):
    # Earlier commits kept a plain list, without the time.
    state = {"forgotten_groups": ["gone-a"]}
    prometheus, warnings = _guard_groups_harness(monkeypatch, state)
    on_server = [{"name": "node-health", "rules": []}]
    prometheus._guard_missing_groups(on_server, None, ["node-health", "gone-a"], False, {})
    assert warnings[-1][1:] == (1, "time unknown")
    assert state["forgotten_groups"] == {"gone-a": "time unknown"}


def test_source_takes_keyword_arguments_only():
    with pytest.raises(TypeError):
        prometheus_source(BASE_URL)


def test_row_text_drops_control_characters():
    # A NUL makes Postgres refuse the whole load, every sync, while the rule exists.
    assert _lines(("Summary", "disk\x00 full\x1b\tnow\nsoon")) == ["Summary: disk full\tnow\nsoon"]
    assert _lines(("Help", "bad \ud800 byte")) == ["Help: bad ? byte"]


def test_rows_do_not_depend_on_the_order_of_labels_or_annotations():
    body = _fixture("rules_first_read.json")
    shuffled = copy.deepcopy(body)
    for group in shuffled["data"]["groups"]:
        for rule in group["rules"]:
            rule["labels"] = dict(reversed(list(rule["labels"].items())))
            if "annotations" in rule:
                rule["annotations"] = dict(reversed(list(rule["annotations"].items())))
    assert _rules(shuffled) == _rules(body)


def test_the_factory_reads_no_environment(dlt_mod, tmp_path, monkeypatch):
    # Environment variables are the example's business, not the factory's: an
    # exported token is neither mixed with explicit basic auth nor sent.
    monkeypatch.setenv("PROMETHEUS_TOKEN", "from-env")
    monkeypatch.setenv("PROMETHEUS_URL", BASE_URL)
    with pytest.raises(ValueError, match="URL required"):
        prometheus_source()
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, username="ops", password="pw")
    ((_, _, headers),) = fake.calls
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"ops:pw").decode()


def test_source_requires_a_url():
    with pytest.raises(ValueError, match="URL required"):
        prometheus_source()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"token": "t", "username": "u", "password": "p"}, "not both"),
        ({"username": "u"}, "both a username and a password"),
        ({"rule_type": "alerting"}, "rule_type must be one of"),
        ({"rule_groups": "node-health"}, "not a single string"),
    ],
)
def test_source_rejects_bad_arguments(kwargs, message, monkeypatch):
    for name in ("PROMETHEUS_USERNAME", "PROMETHEUS_PASSWORD", "PROMETHEUS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match=message):
        prometheus_source(base_url=BASE_URL, **kwargs)


def test_source_selects_only_loadable_resources():
    # The raw API pages are plumbing: if they were loaded, cognee would read them
    # back as documents.
    assert set(prometheus_source(base_url=BASE_URL).resources.selected) == {RULES}
    with_metadata = prometheus_source(base_url=BASE_URL, include_metadata=True)
    assert set(with_metadata.resources.selected) == {
        RULES,
        METRICS,
    }


# ---------------------------------------------------------------------------
# dlt pipeline: snapshot sync, forget-on-delete, fail closed
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch):
    # dlt 1.9 sends anonymous telemetry from a background thread that sleeps
    # between tries. Tests that record time.sleep must only see the connector's.
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")


def _record_sleeps(monkeypatch):
    """Record time.sleep calls made on the main thread, which is where retries wait."""
    real_sleep = time.sleep
    slept = []

    def sleep(seconds):
        if threading.current_thread() is threading.main_thread():
            slept.append(seconds)
        else:
            real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", sleep)
    return slept


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "prometheus.db").as_posix()
    return dlt.pipeline(
        pipeline_name="prometheus_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="prometheus_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run_sync(dlt, tmp_path, fake, base_url=BASE_URL, **source_kwargs):
    """Run prometheus_source through a dlt pipeline into a temp sqlite destination."""
    source_kwargs.setdefault("service", fake.session())
    pipeline = _pipeline(dlt, tmp_path)
    pipeline.run(prometheus_source(base_url=base_url, **source_kwargs))
    return pipeline


def _read(pipeline, table=RULES):
    """Return {id: {"title": ..., "content": ...}} for one staging table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content FROM {table}") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"title": row[1], "content": row[2]} for row in rows}


def test_first_sync_loads_every_rule_in_one_request(dlt_mod, tmp_path):
    fake = FakePrometheus()
    pipeline = _run_sync(dlt_mod, tmp_path, fake)

    rows = _read(pipeline)
    assert len(rows) == 5
    assert sum("Type: recording rule" in row["content"] for row in rows.values()) == 1
    ((path, params, _),) = fake.calls  # metadata is off by default
    assert path == "/api/v1/rules"
    assert params["exclude_alerts"] == ["true"]


def test_unchanged_resync_produces_identical_rows(dlt_mod, tmp_path):
    fake = FakePrometheus(rules=_fixture("rules_first_read.json"))
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    fake.rules = _fixture("rules_second_read.json")
    assert _read(_run_sync(dlt_mod, tmp_path, fake)) == before


def test_one_source_object_run_twice_keeps_its_ids(dlt_mod, tmp_path):
    # A sync loop may hand the same source object to the pipeline again. If the
    # duplicate counter carried over, every id would change and every rule would
    # be forgotten and re-cognified.
    fake = FakePrometheus()
    source = prometheus_source(base_url=BASE_URL, session=fake.session())
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    first = _read(pipeline)
    pipeline.run(source)
    assert _read(pipeline) == first
    assert len(fake.rule_calls()) == 2


def test_group_names_with_braces_are_sent_as_written(dlt_mod, tmp_path):
    # dlt formats query params with str.format, so "{{" would arrive as "{".
    fake = FakePrometheus()
    # allow_empty: the fixture has no such groups, and this test is about the request.
    _run_sync(dlt_mod, tmp_path, fake, rule_groups=["slo:{{svc}}", "slo:{svc}"], allow_empty=True)
    assert fake.rule_calls()[-1][1]["rule_group[]"] == ["slo:{{svc}}", "slo:{svc}"]


def test_edited_rule_changes_exactly_one_row(dlt_mod, tmp_path):
    fake = FakePrometheus()
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    _, rule = _find(fake.rules, "CheckoutHighErrorRate", severity="ticket")
    rule["annotations"]["summary"] = "Checkout 5xx ratio above 1% for 30 minutes"
    after = _read(_run_sync(dlt_mod, tmp_path, fake))

    assert set(after) == set(before)
    changed = [rule_id for rule_id in before if before[rule_id] != after[rule_id]]
    assert len(changed) == 1
    assert "Summary: Checkout 5xx ratio above 1% for 30 minutes" in after[changed[0]]["content"]


def test_removed_rule_is_removed_on_resync(dlt_mod, tmp_path):
    fake = FakePrometheus()
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    group, rule = _find(fake.rules, "HeartbeatAlwaysFiring")
    group["rules"].remove(rule)
    after = _read(_run_sync(dlt_mod, tmp_path, fake))

    assert len(after) == len(before) - 1
    assert not any("HeartbeatAlwaysFiring" in row["title"] for row in after.values())
    assert all(before[rule_id] == row for rule_id, row in after.items())


def test_removed_rule_file_removes_its_rules(dlt_mod, tmp_path):
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake)

    fake.rules["data"]["groups"] = [
        group for group in fake.rules["data"]["groups"] if not group["file"].endswith("infra.yml")
    ]
    after = _read(_run_sync(dlt_mod, tmp_path, fake))
    assert len(after) == 4
    assert not any("ScrapeTargetDown" in row["title"] for row in after.values())


def test_server_that_suddenly_has_no_rules_aborts(dlt_mod, tmp_path):
    # A URL pointing at the wrong server, or a filter that matches nothing,
    # answers with a valid empty list. Under replace that would forget every rule.
    fake = FakePrometheus()
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    fake.rules["data"]["groups"] = []
    with pytest.raises(PipelineStepFailed, match="no rules at all"):
        _run_sync(dlt_mod, tmp_path, fake)

    assert _read(_pipeline(dlt_mod, tmp_path)) == before


def test_allow_empty_lets_a_server_with_no_rules_through(dlt_mod, tmp_path):
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake)

    fake.rules["data"]["groups"] = []
    assert _read(_run_sync(dlt_mod, tmp_path, fake, allow_empty=True)) == {}
    # Once the empty server is accepted, later empty syncs are fine as well.
    _run_sync(dlt_mod, tmp_path, fake)


def test_first_sync_of_a_server_without_rules_is_fine(dlt_mod, tmp_path):
    fake = FakePrometheus(rules={"status": "success", "data": {"groups": []}})
    _run_sync(dlt_mod, tmp_path, fake)  # nothing was synced before, so nothing to protect


def test_filters_hold_when_the_server_ignores_them(dlt_mod, tmp_path):
    # Older Prometheus versions ignore `type` and `rule_group[]` and answer with
    # every group. The connector must not ingest what it was not asked for.
    fake = FakePrometheus()
    fake.ignore_filters = True
    recording = _read(_run_sync(dlt_mod, tmp_path, fake, rule_type="record"))
    assert [row["content"].splitlines()[0] for row in recording.values()] == [
        "Type: recording rule"
    ]
    node = _read(_run_sync(dlt_mod, tmp_path, fake, rule_groups=["node-health"]))
    assert [row["title"] for row in node.values()] == [
        "Prometheus alerting rule ScrapeTargetDown in group node-health"
    ]


def test_sync_stats_count_the_rows_of_the_last_run(dlt_mod, tmp_path):
    fake = FakePrometheus()
    source = prometheus_source(base_url=BASE_URL, service=fake.session(), include_metadata=True)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    assert source.cognee_sync_stats == {
        "rules": len(_read(pipeline)),
        "metrics": len(_read(pipeline, METRICS)),
        "failed_rate_limit": 0,
        "failed_auth": 0,
        "skipped_groups": [],
    }
    assert source.cognee_sync_stats["rules"] > 0


def test_without_a_service_the_capped_retrying_session_is_used(dlt_mod, tmp_path, monkeypatch):
    from cognee_community_connector_prometheus import prometheus

    fake = FakePrometheus()
    made = []

    def capped():
        session = _default_session()
        session.mount("http://", fake)
        made.append(session)
        return session

    monkeypatch.setattr(prometheus, "_default_session", capped)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(prometheus_source(base_url=BASE_URL))
    assert len(made) == 1
    assert _read(pipeline)


def test_counts_are_cleared_when_a_run_fails(dlt_mod, tmp_path):
    fake = FakePrometheus()
    source = prometheus_source(base_url=BASE_URL, service=fake.session(), include_metadata=True)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    assert source.cognee_sync_stats["metrics"] > 0

    fake.fail = lambda path: (503, b"") if path.endswith("/api/v1/metadata") else None
    with pytest.raises(PipelineStepFailed):
        pipeline.run(source)
    assert "metrics" not in source.cognee_sync_stats


def test_session_is_still_accepted_as_the_old_name_of_service(dlt_mod, tmp_path):
    fake = FakePrometheus()
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(prometheus_source(base_url=BASE_URL, session=fake.session()))
    assert _read(pipeline)


def test_a_filter_that_now_matches_nothing_aborts(dlt_mod, tmp_path):
    # The server ignores the filter and answers with groups, but none of them is
    # the one asked for. Checked on the rows left after filtering, that is an
    # empty snapshot, and it must not forget what the last sync loaded.
    fake = FakePrometheus()
    fake.ignore_filters = True
    first = _run_sync(dlt_mod, tmp_path, fake, rule_groups=["node-health"])
    before = _read(first)
    assert before

    for group in fake.rules["data"]["groups"]:
        if group["name"] == "node-health":
            group["name"] = "node-health-v2"
    with pytest.raises(PipelineStepFailed, match="not on the server"):
        _run_sync(dlt_mod, tmp_path, fake, rule_groups=["node-health"])
    assert _read(_pipeline(dlt_mod, tmp_path)) == before


def _session_on(fake):
    session = _default_session()
    session.mount("http://", fake)
    return session


def test_a_429_stops_the_run_at_once(dlt_mod, tmp_path):
    # Waiting out the limit would hold cognee's staging lock, which every other
    # dlt ingestion in the process waits on. The run stops instead.
    fake = FakePrometheus()
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    fake.calls.clear()
    fake.retry_after = "1000"
    fake.fail = lambda path: (429, b"{}")
    source = prometheus_source(base_url=BASE_URL, service=_session_on(fake))
    started = time.monotonic()
    with pytest.raises(PipelineStepFailed, match=r"rate limited \(429\)"):
        _pipeline(dlt_mod, tmp_path).run(source)

    assert time.monotonic() - started < 10, "no Retry-After wait"
    assert len(fake.rule_calls()) == 1, "not retried"
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert _read(_pipeline(dlt_mod, tmp_path)) == before


def test_a_transient_5xx_gets_one_short_retry(dlt_mod, tmp_path, monkeypatch):
    # One blip should not fail the run, but any wait is spent inside cognee's
    # staging lock, so it is one retry, a few seconds.
    slept = _record_sleeps(monkeypatch)
    fake = FakePrometheus()
    fake.retry_after = "1000"
    failed = []

    def once_on_metadata(path):
        if path.endswith("/api/v1/metadata") and not failed:
            failed.append(path)
            return (503, b"")
        return None

    fake.fail = once_on_metadata
    pipeline = _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake), include_metadata=True)
    assert _read(pipeline) and _read(pipeline, METRICS)
    assert failed and slept == [5], "one retry, its 1000 s Retry-After capped at 5 s"


def test_a_5xx_that_persists_stops_the_run_after_one_retry(dlt_mod, tmp_path, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    fake = FakePrometheus()
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    fake.calls.clear()
    fake.retry_after = "1000"
    fake.fail = lambda path: (503, b"")
    with pytest.raises(PipelineStepFailed, match="503"):
        _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert len(fake.rule_calls()) == 2, "one retry"
    assert slept == [5]
    assert _read(_pipeline(dlt_mod, tmp_path)) == before


def test_the_default_session_never_retries_a_429(monkeypatch):
    # Not only through the response hooks: the session itself leaves 429 out of
    # the codes it retries, so a request without the hooks is not retried either.
    slept = _record_sleeps(monkeypatch)
    fake = FakePrometheus()
    fake.retry_after = "1000"
    fake.fail = lambda path: (429, b"{}")
    session = _session_on(fake)
    assert session.get(f"{BASE_URL}/api/v1/rules").status_code == 429
    assert len(fake.calls) == 1 and slept == []

    fake.fail = lambda path: (503, b"")
    fake.calls.clear()
    assert session.get(f"{BASE_URL}/api/v1/rules").status_code == 503
    assert len(fake.calls) == 2 and slept == [5]


def test_a_429_on_the_metadata_call_stops_the_run(dlt_mod, tmp_path, monkeypatch):
    # The metadata call has its own response hooks, so it gets its own test.
    slept = _record_sleeps(monkeypatch)
    fake = FakePrometheus()
    fake.retry_after = "1000"
    fake.fail = lambda path: (429, b"{}") if path.endswith("/api/v1/metadata") else None
    source = prometheus_source(base_url=BASE_URL, service=_session_on(fake), include_metadata=True)
    with pytest.raises(PipelineStepFailed, match=r"rate limited \(429\)"):
        _pipeline(dlt_mod, tmp_path).run(source)
    metadata_calls = [call for call in fake.calls if call[0].endswith("/api/v1/metadata")]
    assert len(metadata_calls) == 1 and slept == []
    assert source.cognee_sync_stats["failed_rate_limit"] == 1


def test_a_dropped_connection_gets_one_short_retry(dlt_mod, tmp_path, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    fake = FakePrometheus()
    dropped = []

    def drop_once(path):
        if not dropped:
            dropped.append(path)
            return requests.ConnectionError("connection reset by peer")
        return None

    fake.fail = drop_once
    assert _read(_run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake)))
    assert dropped and len(slept) == 1 and slept[0] <= 5


def test_a_run_gets_one_retry_in_total(dlt_mod, tmp_path, monkeypatch):
    # Every wait is spent inside cognee's staging lock, so the retry is for the
    # whole run, not one per request: a second blip in the same run stops it.
    slept = _record_sleeps(monkeypatch)
    fake = FakePrometheus()
    fake.retry_after = "1000"
    failed = set()

    def first_try_of_each(path):
        if path not in failed:
            failed.add(path)
            return (503, b"")
        return None

    fake.fail = first_try_of_each
    with pytest.raises(PipelineStepFailed, match="HTTP 503"):
        _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake), include_metadata=True)
    assert slept == [5], "one wait for the run"
    metadata_calls = [call for call in fake.calls if call[0].endswith("/api/v1/metadata")]
    assert len(metadata_calls) == 1, "the second blip of the run is not retried"


def _fail_first_try():
    """A fail() that answers 503 to the first request it sees, then lets everything through."""
    failed = []

    def fail(path):
        if failed:
            return None
        failed.append(path)
        return (503, b"")

    return fail


def test_each_run_starts_with_its_own_retry(dlt_mod, tmp_path, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    fake = FakePrometheus()
    fake.retry_after = "1000"
    source = prometheus_source(base_url=BASE_URL, service=_session_on(fake))
    pipeline = _pipeline(dlt_mod, tmp_path)
    for _ in range(2):  # one blip per run, retried in both runs
        fake.fail = _fail_first_try()
        pipeline.run(source)
    assert slept == [5, 5]
    assert _read(pipeline)


def test_a_request_that_times_out_is_not_retried(dlt_mod, tmp_path, monkeypatch):
    # A hang already cost 30 s under the staging lock; a retry would double it.
    slept = _record_sleeps(monkeypatch)
    fake = FakePrometheus()
    fake.fail = lambda path: requests.ReadTimeout("read timed out")
    with pytest.raises(PipelineStepFailed, match="ReadTimeout failed on /api/v1/rules"):
        _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert len(fake.rule_calls()) == 1 and slept == []


def _error_in(exc):
    """The PrometheusHTTPError somewhere in a pipeline error's chain."""
    from cognee_community_connector_prometheus import PrometheusHTTPError

    seen = getattr(exc, "exception", None) or exc
    while seen is not None:
        if isinstance(seen, PrometheusHTTPError):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_raise_a_named_error_without_the_query(dlt_mod, tmp_path, status):
    from cognee_community_connector_prometheus import PrometheusAuthError

    fake = FakePrometheus()
    fake.fail = lambda path: (status, b"")
    source = prometheus_source(
        base_url=BASE_URL, service=fake.session(), rule_groups=["checkout-availability"]
    )
    with pytest.raises(PipelineStepFailed) as raised:
        _pipeline(dlt_mod, tmp_path).run(source)

    error = _error_in(raised.value)
    assert isinstance(error, PrometheusAuthError) and error.status == status
    assert f"HTTP {status} on /api/v1/rules: check the credentials" in str(raised.value)
    # Group names are the caller's labels; the query that carries them stays out.
    assert "checkout-availability" not in str(raised.value)
    assert "rule_group" not in str(raised.value)
    assert source.cognee_sync_stats["failed_auth"] == 1


def test_a_server_error_names_the_status_and_endpoint_only(dlt_mod, tmp_path):
    fake = FakePrometheus()
    fake.fail = lambda path: (500, b"") if path.endswith("/api/v1/metadata") else None
    with pytest.raises(PipelineStepFailed, match="HTTP 500 on /api/v1/metadata") as raised:
        _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    assert _error_in(raised.value).status == 500


def test_each_request_times_out_after_30_seconds(dlt_mod, tmp_path):
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, service=_session_on(fake))
    assert fake.timeouts and set(fake.timeouts) == {30}


def test_allow_empty_metrics_lets_emptied_metadata_through(dlt_mod, tmp_path):
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)

    fake.metadata = {"status": "success", "data": {}}
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True, allow_empty_metrics=True)
    assert _read(pipeline, METRICS) == {}


def test_allow_empty_for_rules_does_not_wipe_metrics(dlt_mod, tmp_path):
    # Forgetting removed rules on purpose, right after a restart that emptied
    # the metadata, must not take every metric with it.
    fake = FakePrometheus()
    first = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    metrics = _read(first, METRICS)

    fake.rules["data"]["groups"] = []
    fake.metadata = {"status": "success", "data": {}}
    with pytest.raises(PipelineStepFailed, match="under half"):
        _run_sync(dlt_mod, tmp_path, fake, include_metadata=True, allow_empty=True)
    assert _read(_pipeline(dlt_mod, tmp_path), METRICS) == metrics


def test_metadata_that_lost_most_metrics_aborts(dlt_mod, tmp_path):
    # After a restart Prometheus fills metadata in target by target, so a sync in
    # that window sees only some metrics.
    fake = FakePrometheus()
    fake.metadata = {
        "status": "success",
        "data": {f"metric_{n}": [{"type": "gauge", "help": "h", "unit": ""}] for n in range(10)},
    }
    first = _read(_run_sync(dlt_mod, tmp_path, fake, include_metadata=True), METRICS)
    fake.metadata["data"] = dict(list(fake.metadata["data"].items())[:4])
    with pytest.raises(PipelineStepFailed, match="4 metrics, under half of the 10"):
        _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    assert _read(_pipeline(dlt_mod, tmp_path), METRICS) == first

    # Ordinary churn, a few metrics gone, goes through.
    fake.metadata["data"] = {
        f"metric_{n}": [{"type": "gauge", "help": "h", "unit": ""}] for n in range(7)
    }
    assert len(_read(_run_sync(dlt_mod, tmp_path, fake, include_metadata=True), METRICS)) == 7


def test_a_rule_group_asked_for_by_name_that_is_gone_aborts(dlt_mod, tmp_path, monkeypatch):
    fake = FakePrometheus()
    groups = [group["name"] for group in fake.rules["data"]["groups"]][:2]
    before = _read(_run_sync(dlt_mod, tmp_path, fake, rule_groups=groups))

    for group in fake.rules["data"]["groups"]:
        if group["name"] == groups[0]:
            group["name"] = "renamed"
    with pytest.raises(PipelineStepFailed, match="1 of the 2 rule groups"):
        _run_sync(dlt_mod, tmp_path, fake, rule_groups=groups)
    assert _read(_pipeline(dlt_mod, tmp_path)) == before

    # allow_empty forgets it for one run; later runs skip it without the flag.
    kept = _read(_run_sync(dlt_mod, tmp_path, fake, rule_groups=groups, allow_empty=True))
    assert kept and set(kept) < set(before)
    from cognee_community_connector_prometheus import prometheus

    warnings = []
    monkeypatch.setattr(prometheus.logger, "warning", lambda *args: warnings.append(args))
    source = prometheus_source(base_url=BASE_URL, service=fake.session(), rule_groups=groups)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    assert _read(pipeline) == kept
    assert source.cognee_sync_stats["skipped_groups"] == [groups[0]]
    (skipped,) = [args for args in warnings if "skipping" in args[0]]
    assert skipped[1] == 1 and skipped[2].endswith(" UTC")
    # Once the group is back, its rules are synced again.
    for group in fake.rules["data"]["groups"]:
        if group["name"] == "renamed":
            group["name"] = groups[0]
    assert _read(_run_sync(dlt_mod, tmp_path, fake, rule_groups=groups)) == before


def test_metadata_with_warnings_aborts(dlt_mod, tmp_path):
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    partial = {**fake.metadata, "warnings": ["some targets unreachable"]}
    body = json.dumps(partial).encode()
    fake.fail = lambda path: (200, body) if path.endswith("/api/v1/metadata") else None
    with pytest.raises(PipelineStepFailed, match="carries warnings"):
        _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)


def test_a_table_that_empties_logs_a_warning(dlt_mod, tmp_path, monkeypatch):
    from cognee_community_connector_prometheus import prometheus

    warnings = []
    monkeypatch.setattr(prometheus.logger, "warning", lambda *args: warnings.append(args))
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake)
    assert warnings == []
    fake.rules["data"]["groups"] = []
    _run_sync(dlt_mod, tmp_path, fake, allow_empty=True)
    assert len(warnings) == 1 and warnings[0][1] == RULES


def test_names_with_control_characters_are_cleaned_everywhere():
    # Prometheus 3 allows any UTF-8 in names, NUL included.
    body = copy.deepcopy(_fixture("rules_first_read.json"))
    body["data"]["groups"][0]["rules"][0]["name"] = "Scrape\x00Down"
    rows = list(_rule_rows(body["data"]["groups"], Counter()))
    # Check the values themselves: json.dumps would print a NUL as an escape.
    assert all("\x00" not in value for row in rows for value in row.values())
    (row,) = _metric_rows({"weird\x00metric": [{"type": "gauge", "help": "h"}]})
    assert row["id"] == "weirdmetric"
    assert all("\x00" not in value for value in row.values())


def test_metadata_that_is_not_an_object_aborts():
    with pytest.raises(ValueError, match="not an object"):
        list(_metric_rows(["not", "an", "object"]))


@pytest.mark.parametrize("base_url", [f"{BASE_URL}/prom", f"{BASE_URL}/prom/"])
def test_path_prefix_is_kept(dlt_mod, tmp_path, base_url):
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, base_url=base_url)
    assert [path for path, _, _ in fake.calls] == ["/prom/api/v1/rules"]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"token": "tkn"}, "Bearer tkn"),
        (
            {"username": "ops", "password": "s3cret"},
            "Basic " + base64.b64encode(b"ops:s3cret").decode(),
        ),
    ],
)
def test_auth_is_sent_as_configured(dlt_mod, tmp_path, monkeypatch, kwargs, expected):
    for name in ("PROMETHEUS_USERNAME", "PROMETHEUS_PASSWORD", "PROMETHEUS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, headers={"X-Scope-OrgID": "team-a"}, **kwargs)

    ((_, _, headers),) = fake.calls
    assert headers.get("Authorization") == expected
    assert headers["X-Scope-OrgID"] == "team-a"


def test_rule_type_and_group_selection(dlt_mod, tmp_path):
    fake = FakePrometheus()
    recording = _read(_run_sync(dlt_mod, tmp_path, fake, rule_type="record"))
    assert fake.rule_calls()[-1][1]["type"] == ["record"]
    assert [row["title"].split(" rule ")[0] for row in recording.values()] == [
        "Prometheus recording"
    ]

    node = _read(_run_sync(dlt_mod, tmp_path, fake, rule_groups=["node-health"]))
    assert fake.rule_calls()[-1][1]["rule_group[]"] == ["node-health"]
    assert [row["title"] for row in node.values()] == [
        "Prometheus alerting rule ScrapeTargetDown in group node-health"
    ]


def test_metadata_is_opt_in(dlt_mod, tmp_path):
    fake = FakePrometheus()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)

    assert sorted(path for path, _, _ in fake.calls) == ["/api/v1/metadata", "/api/v1/rules"]
    assert set(_read(pipeline, METRICS)) == {
        "prometheus_http_requests_total",
        "prometheus_rule_evaluation_failures_total",
    }


_ERROR = json.dumps({"status": "error", "errorType": "bad_data", "error": "nope"}).encode()


_EMPTY_ERROR = json.dumps({"status": "error", "data": {"groups": []}}).encode()
_NO_METADATA = "an object under 'data'"
_NOT_SUCCESS = "expected status 'success' and a 'data.groups' list"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        pytest.param((503, b"Service Unavailable"), "503", id="http-503"),
        pytest.param((400, _ERROR), "400", id="http-400-error-body"),
        pytest.param((200, _ERROR), _NOT_SUCCESS, id="status-error-with-200"),
        # Shaped like an empty rule list: only the status says it is not one.
        pytest.param((200, _EMPTY_ERROR), _NOT_SUCCESS, id="status-error-with-empty-groups"),
        pytest.param((200, b"<html><body>Sign in</body></html>"), "not JSON", id="html-page"),
        pytest.param(
            (200, b'{"status":"success","data":{"groups":[{"name"'), "not JSON", id="truncated"
        ),
        pytest.param((200, b'{"status":"success","data":{}}'), _NOT_SUCCESS, id="no-groups-key"),
        pytest.param(
            (200, b'{"status":"success","data":{"groups":{}}}'), _NOT_SUCCESS, id="groups-not-list"
        ),
    ],
)
def test_bad_rules_response_aborts_and_keeps_previous_snapshot(dlt_mod, tmp_path, failure, message):
    fake = FakePrometheus()
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    fake.fail = lambda path: failure if path.endswith("/api/v1/rules") else None
    with pytest.raises(PipelineStepFailed, match=re.escape(message)) as raised:
        _run_sync(dlt_mod, tmp_path, fake, rule_groups=["checkout-availability"])
    if failure[0] == 200:
        # The connector's own messages give the path only: the query (the groups
        # filtered on) stays out of logs. dlt's HTTP errors are dlt's to word.
        assert "checkout-availability" not in str(raised.value)

    assert _read(_pipeline(dlt_mod, tmp_path)) == before


def test_check_active_is_called_for_every_response(dlt_mod, tmp_path):
    fake = FakePrometheus()
    calls = []
    _run_sync(dlt_mod, tmp_path, fake, include_metadata=True, check_active=lambda: calls.append(1))
    assert len(calls) == len(fake.calls) == 2


def test_check_active_can_stop_a_sync(dlt_mod, tmp_path):
    # The host revokes access mid-run: the run aborts and memory stays as it was.
    fake = FakePrometheus()
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    def revoked():
        raise PermissionError("access revoked")

    group, rule = _find(fake.rules, "HeartbeatAlwaysFiring")
    group["rules"].remove(rule)
    with pytest.raises(PipelineStepFailed, match="access revoked"):
        _run_sync(dlt_mod, tmp_path, fake, check_active=revoked)

    assert _read(_pipeline(dlt_mod, tmp_path)) == before


def _table_or_empty(pipeline, table):
    try:
        return _read(pipeline, table)
    except DatabaseUndefinedRelation:  # dlt creates no table until a row was loaded
        return {}


def test_server_without_targets_loads_rules_and_no_metrics(dlt_mod, tmp_path):
    # Rules only, or data that arrives by remote write: metadata is always empty.
    fake = FakePrometheus(metadata={"status": "success", "data": {}})
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    assert len(_read(pipeline)) == 5
    assert _table_or_empty(pipeline, METRICS) == {}
    # It keeps working on every run, it is not mistaken for a restart.
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    assert len(_read(pipeline)) == 5


def test_rules_response_with_warnings_aborts(dlt_mod, tmp_path):
    # Thanos Querier answers "success" with only the reachable rulers' groups and a
    # warning when one is down. Taking that as the snapshot would forget the rest.
    fake = FakePrometheus()
    before = _read(_run_sync(dlt_mod, tmp_path, fake))

    partial = copy.deepcopy(fake.rules)
    partial["data"]["groups"] = partial["data"]["groups"][:1]
    partial["warnings"] = ["rule store 10.0.0.2:10901 unreachable"]
    body = json.dumps(partial).encode()
    fake.fail = lambda path: (200, body) if path.endswith("/api/v1/rules") else None
    with pytest.raises(PipelineStepFailed, match="carries warnings"):
        _run_sync(dlt_mod, tmp_path, fake)

    assert _read(_pipeline(dlt_mod, tmp_path)) == before


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        pytest.param((503, b""), "503", id="http-503"),
        pytest.param((200, _ERROR), _NO_METADATA, id="status-error-with-200"),
        pytest.param((200, b'{"status":"success","data":[]}'), _NO_METADATA, id="list"),
        # What a server returns right after a restart, before its first scrapes.
        # The first sync had metrics, so this is refused.
        pytest.param((200, b'{"status":"success","data":{}}'), "under half", id="empty"),
    ],
)
def test_bad_metadata_response_aborts_the_whole_run(dlt_mod, tmp_path, failure, message):
    fake = FakePrometheus()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    rules, metrics = _read(pipeline), _read(pipeline, METRICS)

    fake.fail = lambda path: failure if path.endswith("/api/v1/metadata") else None
    with pytest.raises(PipelineStepFailed, match=re.escape(message)):
        _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)

    pipeline = _pipeline(dlt_mod, tmp_path)
    assert _read(pipeline) == rules
    assert _read(pipeline, METRICS) == metrics


# ---------------------------------------------------------------------------
# cognee: a removed rule leaves the graph, not only staging
# ---------------------------------------------------------------------------

# Distinctive rule names, so each rule maps to exactly one graph entity.
ALPHA = "alphacorp"
BRAVO = "bravocorp"


async def _mock_structured_output(text_input=None, system_prompt=None, response_model=str, **_):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((token for token in (ALPHA, BRAVO) if text_input and token in text_input), None)
        nodes = [KGNode(id=name, name=name, type="Alert", description=name)] if name else []
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token):
    from cognee.infrastructure.databases.graph import get_graph_engine

    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token in str(value).lower() for _, props in nodes for value in (props or {}).values()
    )


def _alert(name):
    return {
        "name": name,
        "query": f'up{{job="{name}"}} == 0',
        "duration": 300,
        "keepFiringFor": 0,
        "labels": {"severity": "page"},
        "annotations": {"summary": f"{name} is down"},
        "type": "alerting",
    }


async def _fake_embed_text(self, text):
    """Zero vectors in whichever shape the installed cognee returns.

    cognee 1.6.x returns a list of vectors; later versions return an
    ``EmbeddingResult`` (a list that also carries a token count).
    """
    vectors = [[0.0] * self.get_vector_size() for _ in text]
    try:
        from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
            EmbeddingResult,
        )
    except ImportError:
        return vectors
    return EmbeddingResult(vectors, tokens=0)


def test_two_servers_in_one_dataset_keep_their_documents(tmp_path, monkeypatch):
    # Each server gets its own tables by default, so a sync of one server never
    # replaces, and orphan-cleans, the other server's documents.
    pytest.importorskip("dlt")
    import asyncio

    import cognee

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    def server(*names):
        group = {"name": "g", "file": "/etc/prometheus/rules/g.yml", "interval": 60}
        group["rules"] = [_alert(name) for name in names]
        return FakePrometheus(rules={"status": "success", "data": {"groups": [group]}})

    first, second = server("alpha_down", "beta_down"), server("gamma_down")

    async def sync(fake, url):
        source = prometheus_source(base_url=url, session=fake.session())
        await cognee.add(source, dataset_name="prometheus_servers")

    async def count():
        datasets = await cognee.datasets.list_datasets()
        found = next(item for item in datasets if item.name == "prometheus_servers")
        return len(await cognee.datasets.list_data(found.id))

    async def scenario():
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            await sync(first, "http://prom-a.test:9090")
            await sync(second, "http://prom-b.test:9090")
            assert await count() == 3
            first.rules["data"]["groups"][0]["rules"].pop()
            await sync(first, "http://prom-a.test:9090")
            assert await count() == 2, "only server A's removed rule may go"
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())


def test_removed_rule_is_forgotten_from_the_graph(tmp_path, monkeypatch):
    # Runs the source through cognee.add() + cognify() (LLM and embeddings mocked,
    # no credentials) to prove the document tag works on this rest_api source and
    # that orphan cleanup reaches the graph.
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")
    import asyncio
    import importlib

    import cognee
    from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
        LiteLLMEmbeddingEngine,
    )
    from cognee.infrastructure.llm import LLMGateway

    add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")
    dataset = "prometheus_forget_test"

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_args, **_kwargs):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _fake_embed_text)

    group = {"name": "uptime", "file": "/etc/prometheus/rules/uptime.yml", "interval": 60}
    group["rules"] = [_alert(ALPHA), _alert(BRAVO)]
    fake = FakePrometheus(rules={"status": "success", "data": {"groups": [group]}})

    async def sync():
        await cognee.add(
            prometheus_source(base_url=BASE_URL, service=fake.session()), dataset_name=dataset
        )
        await cognee.cognify(datasets=[dataset])

    async def data_ids():
        datasets = await cognee.datasets.list_datasets()
        found = next(item for item in datasets if item.name == dataset)
        return {str(item.id) for item in await cognee.datasets.list_data(found.id)}

    async def scenario():
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            await sync()
            assert await _graph_has(ALPHA)
            assert await _graph_has(BRAVO)
            before = await data_ids()
            assert len(before) == 2

            # The rule is deleted from the rule file and Prometheus is reloaded.
            group["rules"] = [_alert(ALPHA)]
            await sync()
            assert await _graph_has(ALPHA), "the surviving rule must stay"
            assert not await _graph_has(BRAVO), "the removed rule must leave the graph"
            after = await data_ids()
            # The surviving rule keeps its document: it was not deleted and re-made.
            assert len(after) == 1 and after < before
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())
