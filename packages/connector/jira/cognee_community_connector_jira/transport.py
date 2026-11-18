"""Read-only, bounded transport on the verified source's dlt HTTP helper."""

import json
import math
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from dlt.sources.helpers import requests


class JiraReadError(RuntimeError):
    """Provider failures without response bodies, URLs or credentials."""


def site_root(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("Invalid Jira site URL") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.atlassian\.net", parsed.hostname)
        or parsed.username
        or parsed.password
        or port
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use an HTTPS Jira Cloud site root and an unscoped API token")
    return f"https://{parsed.hostname}"


def numeric_id(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Jira IDs must be positive decimal identifiers")
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,18}", result):
        raise ValueError("Jira IDs must be positive decimal identifiers")
    return result


def retry_delay(header, attempt: int) -> float:
    try:
        value = float(header)
    except (TypeError, ValueError):
        try:
            value = (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            value = 2**attempt
    if not math.isfinite(value) or value > 60:
        raise JiraReadError("Jira requires a later retry; snapshot aborted")
    return max(0, value)


class Transport:
    def __init__(self, site_url: str, email: str, api_token: str, timeout: float = 30):
        self.site_url = site_root(site_url)
        if not email or not api_token or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Jira credentials and a positive finite timeout are required")
        self.auth = (email, api_token)
        self.timeout = timeout
        # One retry owner: disable the helper's default nested five-attempt loop.
        self.http = requests.Client(request_max_attempts=1, raise_for_status=False)

    def _read(self, path: str, params: dict | None, limit: int) -> bytes:
        if not re.fullmatch(
            r"rest/api/3/(myself|search/jql|issue/[1-9][0-9]*(/comment)?|"
            r"attachment/content/[1-9][0-9]*)",
            path,
        ):
            raise ValueError("Only allowlisted Jira read paths are supported")
        for attempt in range(4):
            started = time.monotonic()
            response = None
            try:
                response = self.http.get(
                    f"{self.site_url}/{path}",
                    auth=self.auth,
                    headers={"Accept": "*/*"},
                    params=params,
                    timeout=(min(5, self.timeout), self.timeout),
                    allow_redirects=False,
                    stream=True,
                )
                status = response.status_code
                if status == 200:
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > limit or time.monotonic() - started > self.timeout:
                            raise JiraReadError("Jira response exceeded byte/time limits")
                        chunks.append(chunk)
                    if time.monotonic() - started > self.timeout:
                        raise JiraReadError("Jira response exceeded time limits")
                    return b"".join(chunks)
                if status not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise JiraReadError(f"Jira returned HTTP {status}; snapshot aborted")
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
            except requests.RequestException:
                if attempt == 3:
                    raise JiraReadError("Jira request failed after four attempts") from None
                delay = 2**attempt
            finally:
                if response is not None:
                    response.close()
            time.sleep(delay)
        raise AssertionError("unreachable")

    def json(self, path: str, params: dict | None = None) -> dict:
        blob = self._read(path, params, 8 * 1024 * 1024)
        try:
            data = json.loads(blob)
        except (ValueError, UnicodeError, RecursionError):
            raise JiraReadError("Jira returned invalid JSON") from None
        if not isinstance(data, dict):
            raise JiraReadError("Jira returned an invalid object")
        return data

    def attachment(self, identity: str, limit: int) -> bytes:
        return self._read(
            f"rest/api/3/attachment/content/{numeric_id(identity)}", {"redirect": "false"}, limit
        )

    def close(self):
        self.http.session.close()
