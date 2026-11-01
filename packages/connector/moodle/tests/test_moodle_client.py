"""The REST client: request shape, parameter encoding, error mapping, retries, token hygiene."""

import pytest
import requests

from cognee_community_connector_moodle import moodle as moodle_module
from cognee_community_connector_moodle.moodle import (
    MoodleAccessError,
    MoodleAPIError,
    MoodleAuthError,
    MoodleClient,
    MoodleNotFoundError,
    _flatten,
)

TOKEN = "SECRETmoodletoken123"
BASE = "https://moodle.example.edu"


class _Response:
    def __init__(self, status: int, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _client(monkeypatch, *responses):
    calls, sleeps = [], []
    queue = list(responses)

    def fake_post(url, data=None, timeout=None):
        calls.append((url, data))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(moodle_module.requests, "post", fake_post)
    return MoodleClient(BASE, TOKEN, sleep=sleeps.append), calls, sleeps


def test_a_call_posts_the_token_and_function_in_the_body_never_in_the_url(monkeypatch):
    client, calls, _ = _client(monkeypatch, _Response(200, {"pages": []}))

    assert client.call("mod_page_get_pages_by_courses", courseids=[7, 8]) == {"pages": []}

    url, data = calls[0]
    assert url == f"{BASE}/webservice/rest/server.php"
    assert TOKEN not in url
    assert data["wstoken"] == TOKEN
    assert data["wsfunction"] == "mod_page_get_pages_by_courses"
    assert data["moodlewsrestformat"] == "json"
    assert data["courseids[0]"] == 7
    assert data["courseids[1]"] == 8


def test_nested_parameters_are_flattened_the_way_moodle_reads_them():
    assert _flatten({"id": 3, "letter": "ALL", "options": {"includenotapproved": True}}) == {
        "id": 3,
        "letter": "ALL",
        "options[includenotapproved]": 1,
    }
    assert _flatten({"tocheck": [{"id": 1, "since": 5}]}) == {
        "tocheck[0][id]": 1,
        "tocheck[0][since]": 5,
    }
    assert _flatten({"skip": None}) == {}


@pytest.mark.parametrize(
    ("errorcode", "error"),
    [
        ("invalidtoken", MoodleAuthError),
        ("accessexception", MoodleAuthError),
        ("nopermissions", MoodleAccessError),
        ("webservice_access_exception", MoodleAccessError),
        ("invalidrecord", MoodleNotFoundError),
        ("invalidcoursemodule", MoodleNotFoundError),
        ("dmlwriteexception", MoodleAPIError),
    ],
)
def test_moodle_exceptions_map_to_typed_errors_without_the_message(monkeypatch, errorcode, error):
    payload = {
        "exception": "moodle_exception",
        "errorcode": errorcode,
        "message": "Secret site text mentioning Ada Lovelace",
    }
    client, _, _ = _client(monkeypatch, _Response(200, payload))

    with pytest.raises(error) as raised:
        client.call("core_course_get_contents", courseid=7)

    assert type(raised.value) is error
    assert errorcode in str(raised.value)
    assert "Ada" not in str(raised.value)
    assert TOKEN not in str(raised.value)


def test_a_non_200_or_unreadable_answer_is_an_error(monkeypatch):
    client, _, _ = _client(monkeypatch, _Response(403, None, text="forbidden"))
    with pytest.raises(MoodleAPIError, match="HTTP 403"):
        client.call("core_webservice_get_site_info")

    client, _, _ = _client(monkeypatch, _Response(200, ValueError("not json"), text="<html>"))
    with pytest.raises(MoodleAPIError, match="unreadable"):
        client.call("core_webservice_get_site_info")


def test_server_errors_and_network_failures_are_retried(monkeypatch):
    client, calls, sleeps = _client(
        monkeypatch,
        _Response(503),
        requests.ConnectionError("boom"),
        _Response(200, {"ok": True}),
    )

    assert client.call("core_webservice_get_site_info") == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_a_persistent_network_failure_raises_without_the_token(monkeypatch):
    client, _, _ = _client(monkeypatch, *[requests.ConnectionError(TOKEN)] * 4)

    with pytest.raises(MoodleAPIError) as raised:
        client.call("core_webservice_get_site_info")

    assert TOKEN not in str(raised.value)
    assert raised.value.__cause__ is None


def test_a_file_download_posts_the_token_and_stays_on_the_site(monkeypatch):
    client, calls, _ = _client(monkeypatch, _Response(200, None, text="<p>Chapter 1</p>"))
    file_url = f"{BASE}/webservice/pluginfile.php/99/mod_book/chapter/5/index.html"

    assert client.download(file_url) == "<p>Chapter 1</p>"
    url, data = calls[0]
    assert url == file_url
    assert data == {"token": TOKEN}

    with pytest.raises(MoodleAccessError, match="outside the site"):
        client.download("https://evil.example.com/webservice/pluginfile.php/x")


def test_a_file_download_that_answers_a_moodle_error_is_mapped(monkeypatch):
    payload = {"exception": "moodle_exception", "errorcode": "nopermissions", "message": "x"}
    client, _, _ = _client(monkeypatch, _Response(200, payload, text='{"exception": "x"}'))

    with pytest.raises(MoodleAccessError):
        client.download(f"{BASE}/webservice/pluginfile.php/99/mod_book/chapter/5/index.html")


def test_the_token_is_never_shown():
    assert TOKEN not in repr(MoodleClient(BASE, TOKEN))


@pytest.mark.parametrize("bad", ["", "   ", "has space", "tab\tkey", "ключ"])
def test_a_malformed_token_is_rejected_without_echoing_it(bad):
    with pytest.raises(ValueError) as raised:
        MoodleClient(BASE, bad)

    assert not bad.strip() or bad not in str(raised.value)


@pytest.mark.parametrize("base", ["http://moodle.example.edu", "moodle.example.edu", ""])
def test_a_remote_base_url_that_is_not_https_is_refused(base):
    with pytest.raises(ValueError, match="https"):
        MoodleClient(base, TOKEN)


@pytest.mark.parametrize(
    "base", ["http://localhost:8000/", "http://127.0.0.1:8000", "http://moodle.test"]
)
def test_a_local_test_site_may_use_plain_http(base):
    client = MoodleClient(base, TOKEN)
    assert client.base_url == base.rstrip("/")
