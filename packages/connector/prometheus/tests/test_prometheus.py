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
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

from cognee_community_connector_prometheus.prometheus import (
    PROMETHEUS_METRICS_TABLE,
    PROMETHEUS_RULES_TABLE,
    PROMETHEUS_SOURCE_NAME,
    _duration,
    _metric_rows,
    _rule_rows,
    prometheus_source,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "http://prometheus.test:9090"


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

    def session(self):
        session = requests.Session()
        session.mount("http://", self)
        return session

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        params = parse_qs(url.query)
        self.calls.append((url.path, params, dict(request.headers)))

        failure = self.fail(url.path) if self.fail else None
        if failure:
            return self._response(request, *failure)
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

    source = prometheus_source(BASE_URL)
    assert PROMETHEUS_SOURCE_NAME == "prometheus"
    assert document_source_tag(source) == "prometheus"


def test_source_rejects_a_url_with_a_query(monkeypatch):
    with pytest.raises(ValueError, match="query string"):
        prometheus_source(f"{BASE_URL}/prom?tenant=a")


def test_source_rejects_credentials_in_the_url():
    # They would end up in dlt's logs and in error messages.
    with pytest.raises(ValueError, match="not in the URL"):
        prometheus_source("http://ops:s3cret@prometheus.test:9090")


@pytest.mark.parametrize("path", ["/api/v1", "/prom/api/v1/"])
def test_source_rejects_the_api_path_in_the_url(path):
    with pytest.raises(ValueError, match="without /api/v1"):
        prometheus_source(f"{BASE_URL}{path}")


def test_source_keeps_its_state_per_dataset():
    # The empty-snapshot flags live in dlt state; without a scope every unscoped
    # dlt source in cognee would share one pipeline state.
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    assert getattr(prometheus_source(BASE_URL), PIPELINE_SCOPE_ATTR) == "prometheus"


def test_explicit_credentials_are_not_mixed_with_the_environment(dlt_mod, tmp_path, monkeypatch):
    # A token exported for something else must not turn explicit basic auth into
    # a "both" error, and must not be sent.
    monkeypatch.setenv("PROMETHEUS_TOKEN", "from-env")
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, username="ops", password="pw")
    ((_, _, headers),) = fake.calls
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"ops:pw").decode()


def test_source_requires_a_url(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    with pytest.raises(ValueError, match="PROMETHEUS_URL"):
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
        prometheus_source(BASE_URL, **kwargs)


def test_source_selects_only_loadable_resources():
    # The raw API pages are plumbing: if they were loaded, cognee would read them
    # back as documents.
    assert set(prometheus_source(BASE_URL).resources.selected) == {PROMETHEUS_RULES_TABLE}
    with_metadata = prometheus_source(BASE_URL, include_metadata=True)
    assert set(with_metadata.resources.selected) == {
        PROMETHEUS_RULES_TABLE,
        PROMETHEUS_METRICS_TABLE,
    }


# ---------------------------------------------------------------------------
# dlt pipeline: snapshot sync, forget-on-delete, fail closed
# ---------------------------------------------------------------------------


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
    pipeline = _pipeline(dlt, tmp_path)
    pipeline.run(prometheus_source(base_url, session=fake.session(), **source_kwargs))
    return pipeline


def _read(pipeline, table=PROMETHEUS_RULES_TABLE):
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
    source = prometheus_source(BASE_URL, session=fake.session())
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(source)
    first = _read(pipeline)
    pipeline.run(source)
    assert _read(pipeline) == first
    assert len(fake.rule_calls()) == 2


def test_group_names_with_braces_are_sent_as_written(dlt_mod, tmp_path):
    # dlt formats query params with str.format, so "{{" would arrive as "{".
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, rule_groups=["slo:{{svc}}", "slo:{svc}"])
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
    with pytest.raises(Exception, match="no rule groups"):
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


def test_allow_empty_also_lets_empty_metadata_through(dlt_mod, tmp_path):
    fake = FakePrometheus()
    _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)

    fake.metadata = {"status": "success", "data": {}}
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True, allow_empty=True)
    assert _read(pipeline, PROMETHEUS_METRICS_TABLE) == {}


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
    assert set(_read(pipeline, PROMETHEUS_METRICS_TABLE)) == {
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
    with pytest.raises(Exception, match=re.escape(message)):
        _run_sync(dlt_mod, tmp_path, fake)

    assert _read(_pipeline(dlt_mod, tmp_path)) == before


def _table_or_empty(pipeline, table):
    try:
        return _read(pipeline, table)
    except Exception:  # dlt creates no table until a row was ever loaded
        return {}


def test_server_without_targets_loads_rules_and_no_metrics(dlt_mod, tmp_path):
    # Rules only, or data that arrives by remote write: metadata is always empty.
    fake = FakePrometheus(metadata={"status": "success", "data": {}})
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    assert len(_read(pipeline)) == 5
    assert _table_or_empty(pipeline, PROMETHEUS_METRICS_TABLE) == {}
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
    with pytest.raises(Exception, match="carries warnings"):
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
        pytest.param(
            (200, b'{"status":"success","data":{}}'), "came back empty after earlier", id="empty"
        ),
    ],
)
def test_bad_metadata_response_aborts_the_whole_run(dlt_mod, tmp_path, failure, message):
    fake = FakePrometheus()
    pipeline = _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)
    rules, metrics = _read(pipeline), _read(pipeline, PROMETHEUS_METRICS_TABLE)

    fake.fail = lambda path: failure if path.endswith("/api/v1/metadata") else None
    with pytest.raises(Exception, match=re.escape(message)):
        _run_sync(dlt_mod, tmp_path, fake, include_metadata=True)

    pipeline = _pipeline(dlt_mod, tmp_path)
    assert _read(pipeline) == rules
    assert _read(pipeline, PROMETHEUS_METRICS_TABLE) == metrics


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

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    group = {"name": "uptime", "file": "/etc/prometheus/rules/uptime.yml", "interval": 60}
    group["rules"] = [_alert(ALPHA), _alert(BRAVO)]
    fake = FakePrometheus(rules={"status": "success", "data": {"groups": [group]}})

    async def sync():
        await cognee.add(prometheus_source(BASE_URL, session=fake.session()), dataset_name=dataset)
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
