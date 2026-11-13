from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from dlt.sources.helpers import requests

from cognee_community_connector_jira.attachments import extract_text
from cognee_community_connector_jira.transport import JiraReadError, Transport, retry_delay


class Response(requests.Response):
    def __init__(self, status=200, body=b"{}", headers=None):
        super().__init__()
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, **kwargs):
        yield self.body

    def close(self):
        self.closed = True


def transport():
    return Transport("https://synthetic.atlassian.net", "owner@example.test", "private-token")


@pytest.mark.parametrize("status", [206, 301, 302, 303, 401, 403, 404])
def test_permanent_status_refused_without_redirect(status):
    client = transport()
    response = Response(status)
    client.http = SimpleNamespace(get=Mock(return_value=response))
    with pytest.raises(JiraReadError, match=f"HTTP {status}"):
        client.attachment("12", 100)
    assert response.closed and client.http.get.call_count == 1
    args = client.http.get.call_args.kwargs
    assert args["allow_redirects"] is False
    assert args["params"] == {"redirect": "false"}


def test_retry_single_owner_and_sanitized_errors():
    client = transport()
    response = Response(429, headers={"Retry-After": "0"})
    # Patch this client's adapter below dlt's wrapped Session.send so helper
    # retries remain active and unrelated background sessions cannot affect counts.
    adapter = client.http.session.get_adapter("https://synthetic.atlassian.net")
    with patch.object(adapter, "send", return_value=response) as send:
        with patch("cognee_community_connector_jira.transport.time.sleep"):
            with pytest.raises(JiraReadError, match="HTTP 429"):
                client.json("rest/api/3/myself")
    assert send.call_count == 4
    client = transport()
    client.http = SimpleNamespace(
        get=Mock(side_effect=requests.ConnectionError("private-token and sensitive response"))
    )
    with patch("cognee_community_connector_jira.transport.time.sleep"):
        with pytest.raises(JiraReadError) as exc:
            client.json("rest/api/3/myself")
    assert "private-token" not in str(exc.value) and client.http.get.call_count == 4


def test_byte_json_route_and_timeout_bounds():
    client = transport()
    response = Response(body=b"12345")
    client.http = SimpleNamespace(get=Mock(return_value=response))
    with pytest.raises(JiraReadError, match="limits"):
        client.attachment("1", 4)
    assert response.closed
    for path in ["https://evil.invalid", "rest/api/3/issue/1/../../secret", "rest/api/3/user"]:
        with pytest.raises(ValueError):
            client.json(path)
    response = Response(body=b"[]")
    client.http.get.return_value = response
    with pytest.raises(JiraReadError, match="invalid object"):
        client.json("rest/api/3/myself")
    response = Response(body=b"broken")
    client.http.get.return_value = response
    with pytest.raises(JiraReadError, match="invalid JSON"):
        client.json("rest/api/3/myself")
    response = Response(body=b"")
    client.http.get.return_value = response
    with patch("cognee_community_connector_jira.transport.time.monotonic", side_effect=[0, 31]):
        with pytest.raises(JiraReadError, match="time"):
            client.attachment("1", 4)


@pytest.mark.parametrize("header", ["NaN", "inf", "61"])
def test_unbounded_retry_after_refused(header):
    with pytest.raises(JiraReadError):
        retry_delay(header, 0)


def test_real_pdf_blank_and_encrypted():
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    blob = io.BytesIO()
    writer.write(blob)
    assert "No extractable text" in extract_text(blob.getvalue(), "application/pdf")
    writer.encrypt("synthetic")
    blob = io.BytesIO()
    writer.write(blob)
    with pytest.raises(ValueError, match="extraction failed"):
        extract_text(blob.getvalue(), "application/pdf")


def test_pdf_deadline():
    import subprocess

    with patch(
        "cognee_community_connector_jira.attachments.subprocess.run",
        side_effect=subprocess.TimeoutExpired("worker", 1),
    ):
        with pytest.raises(ValueError, match="deadline"):
            extract_text(b"pdf", "application/pdf")
