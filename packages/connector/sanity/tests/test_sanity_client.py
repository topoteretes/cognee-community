"""SanityClient: request shape, retries, errors and paging, against a fake HTTP session."""

import pytest

from cognee_community_connector_sanity import SanityAuthError, SanityClient, SanityError
from cognee_community_connector_sanity.sanity import _Scope


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    """Answers each POST from a queue (or a function of the request body)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def post(self, url, params=None, json=None, headers=None, timeout=None, **kwargs):
        self.requests.append(
            {"url": url, "params": params, "json": json, "headers": headers, **kwargs}
        )
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(json)
        if isinstance(answer, Exception):
            raise answer
        return answer


def client(session, **kw):
    return SanityClient("proj1", "production", "tok", session=session, sleep=lambda s: None, **kw)


def test_query_posts_groq_with_params_to_the_uncached_api_in_the_published_view():
    session = Session(Response(200, {"result": [1, 2]}))
    assert client(session).query("*[_type == $t]", {"t": "article"}) == [1, 2]

    request = session.requests[0]
    assert request["url"] == "https://proj1.api.sanity.io/v2025-02-19/data/query/production"
    assert ".apicdn." not in request["url"]
    assert request["params"] == {"perspective": "published"}
    assert request["json"] == {"query": "*[_type == $t]", "params": {"t": "article"}}
    assert request["headers"]["Authorization"] == "Bearer tok"


def test_no_token_sends_no_authorization_header():
    session = Session(Response(200, {"result": []}))
    SanityClient("proj1", "production", None, session=session).query("*")
    assert "Authorization" not in session.requests[0]["headers"]


def test_429_after_every_retry_raises_a_rate_limit_error():
    from cognee_community_connector_sanity.sanity import SanityRateLimitError

    session = Session(*[Response(429)] * 5)
    with pytest.raises(SanityRateLimitError):
        client(session).query("*")
    assert len(session.requests) == 5


def test_429_and_5xx_are_retried_honouring_retry_after():
    sleeps = []
    session = Session(
        Response(429, headers={"Retry-After": "3"}),
        Response(503),
        ConnectionError("reset"),
        Response(200, {"result": "ok"}),
    )
    c = SanityClient("proj1", "production", "tok", session=session, sleep=sleeps.append)
    assert c.query("*") == "ok"
    assert sleeps == [3.0, 2.0, 4.0]


def test_a_long_retry_after_is_capped_at_thirty_seconds():
    sleeps = []
    session = Session(Response(429, headers={"Retry-After": "3600"}), Response(200, {"result": 1}))
    SanityClient("proj1", "production", "tok", session=session, sleep=sleeps.append).query("*")
    assert sleeps == [30.0]


def test_a_dropped_connection_mid_response_is_retried():
    import requests

    session = Session(requests.exceptions.ChunkedEncodingError("cut"), Response(200, {"result": 1}))
    assert client(session).query("*") == 1


def test_a_200_without_json_is_a_sanity_error():
    session = Session(Response(200, ValueError("Expecting value")))
    with pytest.raises(SanityError, match="without a JSON result"):
        client(session).query("*")


def test_redirects_are_not_followed_so_the_token_stays_on_the_api_host():
    session = Session(Response(200, {"result": 1}))
    client(session).query("*")
    assert session.requests[0]["allow_redirects"] is False


def test_a_bad_retry_after_falls_back_to_backoff():
    sleeps = []
    session = Session(Response(429, headers={"Retry-After": "soon"}), Response(200, {"result": 1}))
    SanityClient("p", "production", None, session=session, sleep=sleeps.append).query("*")
    assert sleeps == [1.0]


def test_401_and_403_raise_an_auth_error_without_retrying():
    for status in (401, 403):
        session = Session(Response(status, {"error": {"description": "Unauthorized"}}))
        with pytest.raises(SanityAuthError, match=f"HTTP {status}: Unauthorized"):
            client(session).query("*")
        assert len(session.requests) == 1


def test_a_query_error_raises_with_sanity_s_description():
    session = Session(Response(400, {"error": {"description": "expected ']'"}}))
    with pytest.raises(SanityError, match="HTTP 400: expected"):
        client(session).query("*[")


def test_errors_never_include_the_token():
    session = Session(Response(401, {"error": {"description": "bad"}}))
    with pytest.raises(SanityAuthError) as info:
        client(session).query("*")
    assert "tok" not in str(info.value).replace("token", "")


def test_check_active_runs_before_every_request_including_retries():
    checks = []
    session = Session(Response(503), Response(200, {"result": 1}))
    client(session, check_active=lambda: checks.append(1)).query("*")
    assert len(checks) == 2


def test_check_active_can_stop_the_sync_before_a_request():
    def revoked():
        raise PermissionError("access revoked")

    session = Session(Response(200, {"result": 1}))
    with pytest.raises(PermissionError):
        client(session, check_active=revoked).query("*")
    assert session.requests == []


def test_list_revisions_pages_by_id_until_an_empty_page():
    pages = {
        "": [
            {"_id": "a", "_rev": "1", "_type": "article"},
            {"_id": "b", "_rev": "2", "_type": "article"},
        ],
        "b": [{"_id": "c", "_rev": "3", "_type": "author"}],
        "c": [],
    }
    session = Session(*[lambda body: Response(200, {"result": pages[body["params"]["after"]]})] * 3)
    scope = _Scope(project_id="proj1", dataset="production", document_types=("article", "author"))

    revs = client(session).list_revisions(scope)

    assert revs == {
        "a": {"rev": "1", "type": "article"},
        "b": {"rev": "2", "type": "article"},
        "c": {"rev": "3", "type": "author"},
    }
    first = session.requests[0]["json"]
    assert "_type in $types" in first["query"] and '!(_id in path("_.**"))' in first["query"]
    assert first["params"]["types"] == ["article", "author"]
    assert len(session.requests) == 3, "a short page is not taken as the end"


def test_a_listing_that_repeats_itself_stops_instead_of_looping():
    page = [{"_id": "a", "_rev": "1", "_type": "article"}]
    session = Session(*[Response(200, {"result": page})] * 3)
    scope = _Scope(project_id="proj1", dataset="production", document_types=("article",))
    with pytest.raises(SanityError, match="did not move forward"):
        client(session).list_revisions(scope)
    assert len(session.requests) == 2


def test_groq_filter_is_added_to_the_listing():
    session = Session(Response(200, {"result": []}))
    scope = _Scope(
        project_id="p",
        dataset="production",
        document_types=("article",),
        groq_filter='language == "en"',
    )
    client(session).list_revisions(scope)
    assert '&& (language == "en")' in session.requests[0]["json"]["query"]


def test_documents_and_labels_are_fetched_in_chunks():
    ids = [f"d{i:03}" for i in range(250)]
    session = Session(*[Response(200, {"result": []})] * 3)
    client(session).get_documents(ids)
    assert [len(r["json"]["params"]["ids"]) for r in session.requests] == [100, 100, 50]


def test_labels_use_the_first_title_like_field():
    session = Session(
        Response(
            200,
            {
                "result": [
                    {"_id": "a", "_rev": "1", "_type": "author", "name": "Alice"},
                    {"_id": "t", "_rev": "2", "_type": "tag", "slug": "billing"},
                    {"_id": "x", "_rev": "3", "_type": "thing"},
                ]
            },
        )
    )
    labels = client(session).get_labels(["a", "t", "x"])
    assert {k: v["label"] for k, v in labels.items()} == {"a": "Alice", "t": "billing", "x": "x"}


def test_schema_is_read_in_the_raw_view():
    session = Session(Response(200, {"result": None}))
    assert client(session).get_schema() is None
    assert session.requests[0]["params"] == {"perspective": "raw"}
    assert session.requests[0]["json"]["params"] == {"id": "_.schemas.default"}


def test_repr_never_shows_the_token():
    c = SanityClient("proj1", "production", "sk-secret-token")
    assert "sk-secret" not in repr(c) and "proj1" in repr(c)
