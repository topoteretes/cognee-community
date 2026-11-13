"""GA4Client against a fake discovery service: paging, version routing, errors and retries."""

import httplib2
import pytest
from fake_ga4 import FakeAdminService, http_error
from google.auth.exceptions import RefreshError, TransportError

from cognee_community_connector_ga4.ga4 import (
    GA4AccessError,
    GA4APIError,
    GA4AuthError,
    GA4Client,
    GA4RateLimitedError,
    load_credentials,
)


def _client(beta: FakeAdminService, alpha: FakeAdminService | None = None) -> tuple:
    sleeps: list[float] = []
    client = GA4Client({"v1beta": beta, "v1alpha": alpha or beta}, sleep=sleeps.append)
    return client, sleeps


def test_a_listing_follows_page_tokens_at_the_largest_page_size():
    beta = FakeAdminService()
    beta.pages["customDimensions"] = {
        None: {"customDimensions": [{"name": "a"}], "nextPageToken": "t1"},
        "t1": {"customDimensions": [{"name": "b"}], "nextPageToken": "t2"},
        "t2": {"customDimensions": [{"name": "c"}]},
    }
    client, _ = _client(beta)

    assert client.list("custom_dimensions", "42") == [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    params = [params for _, params, _ in beta.executed]
    assert params[0] == {"parent": "properties/42", "pageSize": 200}
    assert [p.get("pageToken") for p in params] == [None, "t1", "t2"]
    assert {retries for *_, retries in beta.executed} == {0}


def test_an_empty_listing_answers_an_empty_list():
    beta = FakeAdminService()
    beta.pages["keyEvents"] = {None: {}}

    assert _client(beta)[0].list("key_events", "42") == []


def test_audiences_and_annotations_are_read_from_v1alpha():
    beta, alpha = FakeAdminService(), FakeAdminService()
    alpha.pages["audiences"] = {None: {"audiences": [{"name": "x"}]}}
    alpha.pages["reportingDataAnnotations"] = {None: {"reportingDataAnnotations": []}}
    client, _ = _client(beta, alpha)

    client.list("audiences", "42")
    client.list("annotations", "42")

    assert [c for c, *_ in alpha.executed] == ["audiences", "reportingDataAnnotations"]
    assert beta.executed == []


def test_a_page_token_that_repeats_is_refused_instead_of_paging_forever():
    beta = FakeAdminService()
    beta.pages["customMetrics"] = {
        None: {"customMetrics": [], "nextPageToken": "same"},
        "same": {"customMetrics": [], "nextPageToken": "same"},
    }

    with pytest.raises(GA4APIError, match="did not advance"):
        _client(beta)[0].list("custom_metrics", "42")


@pytest.mark.parametrize("page", [None, [], {"customMetrics": {"name": "a"}}])
def test_an_unexpected_page_shape_is_refused(page):
    beta = FakeAdminService()
    beta.pages["customMetrics"] = {None: page}

    with pytest.raises(GA4APIError, match="unexpected shape"):
        _client(beta)[0].list("custom_metrics", "42")


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (http_error(401, "UNAUTHENTICATED"), GA4AuthError, "rejected the credentials"),
        (
            http_error(403, "PERMISSION_DENIED", "SERVICE_DISABLED"),
            GA4AccessError,
            "Admin API is not enabled",
        ),
        (http_error(403, "PERMISSION_DENIED"), GA4AccessError, "as a Viewer"),
        (http_error(429, "RESOURCE_EXHAUSTED"), GA4RateLimitedError, "quota exhausted"),
        (http_error(400, "INVALID_ARGUMENT"), GA4APIError, "HTTP 400 INVALID_ARGUMENT"),
    ],
)
def test_google_errors_map_to_source_errors_without_echoing_the_body(error, expected, message):
    beta = FakeAdminService()
    beta.failures = [error]

    with pytest.raises(expected, match=message) as raised:
        _client(beta)[0].list("custom_dimensions", "42")

    assert "must not leak" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_a_body_that_is_not_json_still_maps_by_status():
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    beta = FakeAdminService()
    beta.failures = [HttpError(Response({"status": 403}), b"<html>forbidden</html>")]

    with pytest.raises(GA4AccessError, match="as a Viewer"):
        _client(beta)[0].list("custom_dimensions", "42")


def test_server_errors_and_network_errors_are_retried_with_backoff():
    beta = FakeAdminService()
    beta.pages["keyEvents"] = {None: {"keyEvents": [{"name": "k"}]}}
    beta.failures = [http_error(503, "UNAVAILABLE"), TransportError("connection reset")]
    client, sleeps = _client(beta)

    assert client.list("key_events", "42") == [{"name": "k"}]
    assert sleeps == [1, 2]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (http_error(500, "INTERNAL"), "HTTP 500 INTERNAL"),
        (httplib2.HttpLib2Error("boom"), "HttpLib2Error"),
        (OSError("timed out"), "OSError"),
    ],
)
def test_a_failure_that_outlasts_the_retries_raises(failure, message):
    beta = FakeAdminService()
    beta.failures = [failure, failure, failure]

    with pytest.raises(GA4APIError, match=message):
        _client(beta)[0].list("key_events", "42")


def test_a_revoked_key_is_an_auth_error_and_is_not_retried():
    beta = FakeAdminService()
    beta.failures = [RefreshError("invalid_grant: Invalid JWT Signature.")]

    with pytest.raises(GA4AuthError, match="service-account key"):
        _client(beta)[0].list("key_events", "42")
    assert len(beta.executed) == 1


def test_the_client_repr_hides_its_credentials():
    assert repr(GA4Client({})) == "GA4Client(credentials=<redacted>)"


def test_missing_application_default_credentials_explain_what_to_set(monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def no_credentials(scopes=None):
        raise DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(google.auth, "default", no_credentials)

    with pytest.raises(GA4AuthError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        load_credentials()


def test_a_key_file_is_loaded_with_the_read_only_scope(service_account_file):
    credentials = load_credentials(str(service_account_file))

    assert credentials.service_account_email == "ga4-reader@example-project.iam.gserviceaccount.com"
    assert credentials.scopes == ["https://www.googleapis.com/auth/analytics.readonly"]


def test_a_missing_key_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_credentials(str(tmp_path / "missing.json"))
