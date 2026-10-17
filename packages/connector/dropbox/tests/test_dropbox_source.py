"""dropbox_source() factory: configuration, validation and resource wiring."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_dropbox import FakeDropbox

from cognee_community_connector_dropbox import dropbox_source
from cognee_community_connector_dropbox.dropbox import _normalize_folder_paths


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


def test_credentials_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DROPBOX_REFRESH_TOKEN", "token")
    monkeypatch.setenv("DROPBOX_APP_KEY", "key")

    dropbox_source()  # builds a client without any network call


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
