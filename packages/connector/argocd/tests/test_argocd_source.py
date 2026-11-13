"""Factory validation, the HTTP client and the cognee seams. No network, no LLM."""

from uuid import uuid4

import pytest
import requests
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_row_data import DltRowData
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.extract.exceptions import ResourceExtractionError
from fake_argocd import FakeArgoSession, FakeResponse, make_app

from cognee_community_connector_argocd import argocd, argocd_source
from cognee_community_connector_argocd.argocd import _ArgoConfig, _get_json, _iter_rows

TOKEN = "s3cret-token-value"
URL = "https://argocd.example.com"


class RecordingLogger:
    """Stands in for the module logger so tests can read what was logged."""

    def __init__(self):
        self.lines: list[str] = []

    def _record(self, message, *args):
        self.lines.append(message % args if args else message)

    warning = info = debug = _record

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture(autouse=True)
def log(monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(argocd, "logger", recorder)
    monkeypatch.setattr(argocd, "_warned", set())
    monkeypatch.setattr(argocd, "_sleep", lambda seconds: None)
    for name in (
        "ARGOCD_BASE_URL",
        "ARGOCD_AUTH_TOKEN",
        "ARGOCD_INSTANCE_NAME",
        "ARGOCD_PROJECTS",
        "ARGOCD_APPS",
    ):
        monkeypatch.delenv(name, raising=False)
    return recorder


def build(**overrides):
    kwargs = {"base_url": URL, "api_token": TOKEN, "session": FakeArgoSession()}
    kwargs.update(overrides)
    return argocd_source(**kwargs)


def config(**overrides) -> _ArgoConfig:
    values = {
        "base_url": URL,
        "instance": "prod",
        "projects": (),
        "app_names": (),
        "app_namespace": None,
        "selector": None,
        "include_resources": True,
        "include_history": True,
        "history_limit": None,
        "include_helm_values": False,
        "verify": True,
        "timeout": 60.0,
    }
    values.update(overrides)
    return _ArgoConfig(**values)


# ---------------------------------------------------------------------------
# Factory validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"base_url": None}, "base_url is required"),
        ({"base_url": "ftp://argocd.example.com"}, "must look like"),
        ({"base_url": "argocd.example.com"}, "must look like"),
        ({"base_url": "http://argocd.example.com"}, "https://"),
        ({"base_url": "https://user:pw@argocd.example.com"}, "must not contain credentials"),
        ({"base_url": f"https://argocd.example.com/?token={TOKEN}"}, "query or fragment"),
        ({"base_url": "https://argocd.example.com/#frag"}, "query or fragment"),
        ({"base_url": "https://argocd.example.com:notaport"}, "not a valid URL"),
        ({"api_token": None}, "API token is required"),
        ({"history_limit": 0}, "history_limit"),
        ({"timeout": 0}, "timeout"),
        ({"allow_mass_delete": "yes"}, "allow_mass_delete"),
        ({"ca_bundle": "/no/such/ca.pem"}, "existing file"),
        ({"projects": []}, "projects is empty"),
        ({"app_names": [" "]}, "app_names is empty"),
        ({"instance_name": "bad name/with:chars"}, "instance_name may only contain"),
    ],
)
def test_invalid_configuration_is_refused_without_echoing_secrets(overrides, message):
    with pytest.raises(ValueError, match=message) as error:
        build(**overrides)
    assert TOKEN not in str(error.value)
    assert "pw@" not in str(error.value)


def test_verify_ssl_false_with_a_ca_bundle_is_a_contradiction(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("pem")
    with pytest.raises(ValueError, match="contradict"):
        build(verify_ssl=False, ca_bundle=str(bundle))


def test_localhost_needs_an_instance_name():
    with pytest.raises(ValueError, match="instance_name is required"):
        build(base_url="https://localhost:8080")
    build(base_url="https://localhost:8080", instance_name="demo")


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1:8080", "[::1]:8080"])
def test_plain_http_is_allowed_only_for_loopback_and_warns_once(host, log):
    build(base_url=f"http://{host}", instance_name="demo")
    build(base_url=f"http://{host}", instance_name="demo")
    assert log.text.count("plain http://") == 1


def test_instance_name_defaults_to_the_host_name():
    resource = build()
    source_state = {}
    rows = list(
        _iter_rows(
            FakeArgoSession([make_app()]), config(instance="argocd.example.com"), source_state
        )
    )
    assert rows[0]["id"].startswith("argocd:argocd.example.com:")
    assert resource.name == "argocd_applications"


def test_url_with_a_path_prefix_and_trailing_slash_keeps_the_prefix():
    session = FakeArgoSession()
    resource = build(base_url="https://gateway.example.com/argo-cd/", session=session)
    list(resource)
    assert session.calls[0]["url"] == "https://gateway.example.com/argo-cd/api/v1/applications"


def test_environment_variables_are_the_fallback(monkeypatch):
    monkeypatch.setenv("ARGOCD_BASE_URL", "https://localhost:8080")
    monkeypatch.setenv("ARGOCD_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ARGOCD_INSTANCE_NAME", "demo")
    monkeypatch.setenv("ARGOCD_PROJECTS", "payments, platform")
    monkeypatch.setenv("ARGOCD_APPS", "guestbook")
    session = FakeArgoSession()
    list(argocd_source(session=session))
    params = session.calls[0]["params"]
    assert ("projects", "payments") in params
    assert ("projects", "platform") in params
    assert ("names", "guestbook") in params


def test_arguments_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("ARGOCD_PROJECTS", "from-env")
    session = FakeArgoSession()
    list(build(projects=["from-arg"], session=session))
    assert session.calls[0]["params"] == [("projects", "from-arg")]


def test_verify_ssl_false_warns_once(log):
    build(verify_ssl=False)
    build(verify_ssl=False)
    assert log.text.count("TLS verification is turned off") == 1


def test_token_never_appears_in_logs_or_stats(log):
    resource = build(session=FakeArgoSession([make_app()]))
    list(resource)
    assert TOKEN not in log.text
    assert TOKEN not in str(resource.cognee_sync_stats)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def get(session, **kwargs):
    return _get_json(session, f"{URL}/api/v1/applications", **kwargs)


def test_retries_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(argocd, "_sleep", sleeps.append)
    session = FakeArgoSession()
    session.script = [FakeResponse(503, {}), FakeResponse(429, {}), FakeResponse(502, {})]
    assert get(session) == {"metadata": {"resourceVersion": "1"}, "items": []}
    assert len(session.calls) == 4
    assert len(sleeps) == 3
    assert sleeps[1] > sleeps[0] - 1  # exponential, plus jitter of at most 1s


def test_retry_after_header_is_honoured(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(argocd, "_sleep", sleeps.append)
    session = FakeArgoSession()
    session.script = [FakeResponse(429, {}, {"retry-after": "7"})]
    get(session)
    assert sleeps == [7.0]


def test_retry_exhaustion_raises_with_the_status():
    session = FakeArgoSession()
    session.script = [FakeResponse(503, {"message": "overloaded"})] * 6
    with pytest.raises(RuntimeError, match="HTTP 503") as error:
        get(session)
    assert "overloaded" in str(error.value)
    assert len(session.calls) == 6  # the first try and five retries


def test_401_explains_how_to_create_a_token():
    session = FakeArgoSession()
    session.script = [FakeResponse(401, {"message": "invalid session: token is expired"})]
    with pytest.raises(RuntimeError, match="generate-token") as error:
        get(session)
    assert "expired" in str(error.value)
    assert len(session.calls) == 1


def test_403_names_the_minimum_policy():
    session = FakeArgoSession()
    session.script = [FakeResponse(403, {"message": "permission denied"})]
    with pytest.raises(RuntimeError, match=r"applications, get, \*/\*, allow"):
        get(session)


def test_404_points_at_the_base_path():
    session = FakeArgoSession()
    session.script = [FakeResponse(404, "<html>nope</html>")]
    with pytest.raises(RuntimeError, match="rootpath"):
        get(session)


def test_other_status_includes_the_server_message():
    session = FakeArgoSession()
    session.script = [FakeResponse(500, {"message": "boom"})]
    with pytest.raises(RuntimeError, match=r"HTTP 500.*boom"):
        get(session)


def test_a_non_json_body_is_explained():
    session = FakeArgoSession()
    session.script = [FakeResponse(200, "<html>login</html>")]
    with pytest.raises(RuntimeError, match="not JSON"):
        get(session)


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (requests.exceptions.ConnectionError("refused"), "port-forward"),
        (requests.exceptions.SSLError("bad cert"), "ca_bundle"),
        (requests.exceptions.ReadTimeout("slow"), "timed out"),
    ],
)
def test_connection_problems_come_with_a_hint(error, hint):
    session = FakeArgoSession()
    session.script = [error]
    with pytest.raises(RuntimeError, match=hint):
        get(session)


def test_verify_and_timeout_are_passed_to_every_request(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("pem")
    session = FakeArgoSession()
    list(build(session=session, ca_bundle=str(bundle), timeout=12.5))
    assert session.calls[0]["verify"] == str(bundle)
    assert session.calls[0]["timeout"] == 12.5

    default = FakeArgoSession()
    list(build(session=default))
    assert default.calls[0]["verify"] is True


def test_filters_are_sent_as_repeated_query_parameters():
    session = FakeArgoSession()
    list(
        build(
            session=session,
            projects=["a", "b"],
            app_names=["argocd/x", "y"],
            selector="team=payments",
            app_namespace="argocd",
        )
    )
    assert session.calls[0]["params"] == [
        ("projects", "a"),
        ("projects", "b"),
        ("names", "argocd/x"),
        ("names", "y"),
        ("selector", "team=payments"),
        ("appNamespace", "argocd"),
    ]


def test_no_resource_version_or_refresh_parameter_is_ever_sent():
    session = FakeArgoSession()
    list(build(session=session))
    names = {key for key, _ in session.calls[0]["params"]}
    assert names.isdisjoint({"resourceVersion", "refresh"})


def test_the_default_session_sends_a_bearer_token():
    session = argocd._make_session(TOKEN)
    assert session.headers["Authorization"] == f"Bearer {TOKEN}"
    assert session.headers["Accept"] == "application/json"


def test_null_items_mean_no_applications():
    session = FakeArgoSession()
    session.script = [FakeResponse(200, {"metadata": {}, "items": None})]
    assert list(_iter_rows(session, config(), {})) == []


# ---------------------------------------------------------------------------
# cognee seams
# ---------------------------------------------------------------------------
def test_resource_opts_into_document_mode_and_pipeline_scope():
    resource = build(resource_name="prod_argocd")
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "argocd"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "prod_argocd"
    assert resource.cognee_sync_stats == {}
    assert resource.name == "prod_argocd"


def test_resource_declares_merge_with_a_hard_delete_column():
    resource = build()
    schema = resource.compute_table_schema()
    disposition = schema["write_disposition"]
    assert (
        disposition.get("disposition") if isinstance(disposition, dict) else disposition
    ) == "merge"
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["columns"]["_deleted"]["hard_delete"] is True


def test_old_cognee_without_document_sync_is_refused(monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match="table-scoped"):
        build()


def test_rows_pass_through_guarded_rows():
    calls: list[str] = []
    resource = build(
        session=FakeArgoSession([make_app()]), check_active=lambda: calls.append("check")
    )
    rows = list(resource)
    assert len(rows) == 1
    assert len(calls) >= 3  # before and after the row, and at the end


def test_check_active_can_stop_the_run():
    def stop():
        raise PermissionError("revoked")

    # dlt wraps whatever a resource raises while it is being extracted.
    with pytest.raises(ResourceExtractionError, match="revoked"):
        list(build(session=FakeArgoSession([make_app()]), check_active=stop))


def test_rows_become_cognee_document_items():
    row = next(_iter_rows(FakeArgoSession([make_app()]), config(), {}))
    # The fields cognee reads from a loaded row: id, title, content (url is optional).
    assert set(row) == {"id", "title", "content", "_deleted"}
    assert row["_deleted"] is False
    loaded = DltRowData(
        table_name="argocd_applications",
        primary_key_column="id",
        primary_key_value=row["id"],
        row_data=row,
        content_hash="h",
        schema_info=[],
        schema_hash="s",
        foreign_keys=[],
        dlt_db_name="db",
        dataset_name="ds",
    )
    item = _build_document_data_item(loaded, uuid4(), "argocd")
    assert item.system_metadata["source"] == "argocd"
    assert item.system_metadata["external_id"] == row["id"]
    assert item.data.startswith("# Argo CD application guestbook")
