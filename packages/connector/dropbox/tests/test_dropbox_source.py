"""dropbox_source() factory: configuration, validation and resource wiring."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_dropbox import FakeDropbox

from cognee_community_connector_dropbox import dropbox as dropbox_module
from cognee_community_connector_dropbox import dropbox_source
from cognee_community_connector_dropbox.dropbox import (
    _normalize_folder_paths,
    build_dropbox_client,
)


@pytest.fixture(autouse=True)
def no_dropbox_env(monkeypatch):
    for name in (
        "DROPBOX_ACCESS_TOKEN",
        "DROPBOX_REFRESH_TOKEN",
        "DROPBOX_APP_KEY",
        "DROPBOX_APP_SECRET",
        "DROPBOX_FOLDER_PATHS",
        "DROPBOX_MAX_FILE_SIZE_MB",
    ):
        monkeypatch.delenv(name, raising=False)


def test_resource_opts_into_document_mode_and_pipeline_scope():
    resource = dropbox_source("/Notes", resource_name="work_dropbox", client=FakeDropbox())

    assert resource.name == "work_dropbox"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "dropbox"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "work_dropbox"
    assert resource.cognee_sync_stats == {}


def test_missing_credentials_fail_when_the_source_is_built():
    with pytest.raises(ValueError, match="credentials are missing"):
        dropbox_source()


def test_refresh_token_without_app_key_fails():
    with pytest.raises(ValueError, match="needs the app key"):
        dropbox_source(refresh_token="token")


def test_refresh_token_and_app_key_build_a_client_without_a_network_call():
    resource = dropbox_source(refresh_token="token", app_key="key")

    assert resource.name == "dropbox_files"


def test_environment_is_left_to_the_example(monkeypatch):
    monkeypatch.setenv("DROPBOX_REFRESH_TOKEN", "token")
    monkeypatch.setenv("DROPBOX_APP_KEY", "key")

    with pytest.raises(ValueError, match="credentials are missing"):
        dropbox_source()


def test_service_is_the_injected_client_and_client_still_works():
    fake = FakeDropbox()

    assert dropbox_source(service=fake) is not None
    assert dropbox_source(client=fake) is not None


@pytest.mark.parametrize(
    "name", ["", "x" * 65, "Work-Dropbox", "work dropbox", "1dropbox", "work__dbx", "work_", "x__"]
)
def test_resource_name_that_would_lose_or_share_state_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        dropbox_source(resource_name=name, service=FakeDropbox())


def test_empty_folder_list_is_refused_instead_of_forgetting_everything():
    with pytest.raises(ValueError, match="folder_paths is empty"):
        dropbox_source([], client=FakeDropbox())


def test_folder_paths_are_normalized_and_nested_folders_dropped():
    assert _normalize_folder_paths(["/Notes", "notes/Sub/", "/Work/", "/notes2"]) == (
        "/notes",
        "/notes2",
        "/work",
    )
    assert _normalize_folder_paths(["/", "/Anything"]) == ("",)


def test_refresh_token_goes_to_the_sdk_as_a_refresh_token(monkeypatch):
    built: list[dict] = []
    monkeypatch.setattr(dropbox_module, "Dropbox", lambda **kwargs: built.append(kwargs))

    build_dropbox_client(refresh_token="refresh", app_key="key", app_secret="secret")
    build_dropbox_client(access_token="access")

    assert built[0]["oauth2_refresh_token"] == "refresh"
    assert "oauth2_access_token" not in built[0]
    assert built[1]["oauth2_access_token"] == "access"
    assert all(kwargs["max_retries_on_rate_limit"] == 0 for kwargs in built)
    # The SDK's own 5xx retries sleep without asking check_active.
    assert all(kwargs["max_retries_on_error"] == 0 for kwargs in built)


@pytest.mark.parametrize("size", [0, -1, True, float("nan"), "25"])
def test_a_file_size_limit_that_would_skip_everything_is_refused(size):
    with pytest.raises(ValueError, match="max_file_size_mb"):
        dropbox_source(max_file_size_mb=size, service=FakeDropbox())


def test_names_dlt_keeps_as_they_are_are_accepted():
    for name in ("dropbox_files", "work_dbx", "a1_b2", "dbx"):
        assert dropbox_source(resource_name=name, service=FakeDropbox()).name == name


def test_each_run_builds_its_own_client_and_closes_it(monkeypatch, tmp_path):
    import dlt

    built: list[FakeDropbox] = []

    def fake_sdk(**kwargs):
        built.append(FakeDropbox(**kwargs))
        built[-1].put("/a.md", "alpha")
        return built[-1]

    # The real build_dropbox_client runs; only the SDK class is replaced.
    monkeypatch.setattr(dropbox_module, "Dropbox", fake_sdk)
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path))
    resource = dropbox_source(refresh_token="refresh", app_key="key")
    pipeline = dlt.pipeline("dropbox_close_test", dev_mode=True)

    pipeline.extract(resource)
    pipeline.extract(resource)

    assert len(built) == 2, "a client per run, not one shared across runs"
    assert all(client.closed for client in built)


def test_an_injected_client_is_never_closed(monkeypatch, tmp_path):
    import dlt

    service = FakeDropbox()
    service.put("/a.md", "alpha")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path))

    dlt.pipeline("dropbox_injected_test", dev_mode=True).extract(dropbox_source(service=service))

    assert not service.closed
