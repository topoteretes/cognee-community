"""Postman API v10 HTTP client with rate-limiting resilience and error mapping."""

from __future__ import annotations

import os
import time
import urllib.parse
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("postman_client")
except ImportError:
    import logging

    logger = logging.getLogger("postman_client")

# Retry budget for rate-limited / transient Postman API responses.
_MAX_RETRIES: int = 5

# HTTP status codes eligible for retry with backoff.
_TRANSIENT_STATUS_CODES: tuple[int, ...] = (429, 500, 502, 503, 504)


class PostmanError(Exception):
    """Base exception for all Postman connector errors."""


class PostmanAPIError(PostmanError):
    """Exception raised when Postman API returns an error response."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_name: str | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_name = error_name
        self.response_body = response_body


class PostmanNotFoundError(PostmanAPIError, KeyError):
    """Exception raised when a requested Postman resource is not found (HTTP 404).

    Inherits from KeyError to satisfy both KeyError and PostmanNotFoundError contracts.
    """

    def __init__(
        self,
        message: str = "Postman resource not found",
        status_code: int = 404,
        response_body: Any = None,
    ) -> None:
        PostmanAPIError.__init__(
            self,
            message,
            status_code=status_code,
            error_name="instanceNotFoundError",
            response_body=response_body,
        )
        KeyError.__init__(self, message)


class PostmanAuthenticationError(PostmanAPIError, PermissionError):
    """Exception raised when Postman API authentication fails (HTTP 401).

    Inherits from PermissionError.
    """

    def __init__(
        self,
        message: str = "Invalid or missing Postman API key",
        status_code: int = 401,
        response_body: Any = None,
    ) -> None:
        PostmanAPIError.__init__(
            self,
            message,
            status_code=status_code,
            error_name="AuthenticationError",
            response_body=response_body,
        )
        PermissionError.__init__(self, message)


class PostmanPermissionError(PostmanAPIError, PermissionError):
    """Exception raised when Postman API access is forbidden (HTTP 403).

    Inherits from PermissionError.
    """

    def __init__(
        self,
        message: str = "Forbidden: Access denied to Postman resource",
        status_code: int = 403,
        response_body: Any = None,
    ) -> None:
        PostmanAPIError.__init__(
            self,
            message,
            status_code=status_code,
            error_name="forbiddenError",
            response_body=response_body,
        )
        PermissionError.__init__(self, message)


class PostmanRateLimitError(PostmanAPIError):
    """Exception raised when Postman API rate limit retries are exhausted (HTTP 429)."""

    def __init__(
        self,
        message: str = "Postman API rate limit exceeded",
        status_code: int = 429,
        retry_after: float | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            error_name="rateLimitError",
            response_body=response_body,
        )
        self.retry_after = retry_after


def _retry_after(headers: Any, attempt: int) -> float:
    """Return seconds to wait before retrying: Retry-After header, else exponential backoff.

    Parses Retry-After header (float/int seconds). If header is missing or unparseable,
    falls back to exponential backoff 2 ** attempt seconds.
    """
    header_val: Any = None
    if headers is not None:
        if hasattr(headers, "get"):
            header_val = headers.get("retry-after") or headers.get("Retry-After")
        elif isinstance(headers, list | tuple):
            for k, v in headers:
                if str(k).lower() == "retry-after":
                    header_val = v
                    break

    try:
        val = float(header_val)
        if val >= 0:
            return val
    except (TypeError, ValueError):
        pass

    return float(2**attempt)


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit, server, timeout, and network errors worth retrying."""
    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "status", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
        or getattr(exc, "code", None)
    )
    if status in _TRANSIENT_STATUS_CODES:
        return True

    # Standard library timeout and connection errors
    if isinstance(exc, TimeoutError | ConnectionError):
        return True

    # Check for requests exception types if requests is present
    try:
        import requests

        if isinstance(exc, requests.exceptions.Timeout | requests.exceptions.ConnectionError):
            return True
        if isinstance(exc, requests.exceptions.HTTPError):
            resp = getattr(exc, "response", None)
            if resp is not None and getattr(resp, "status_code", None) in _TRANSIENT_STATUS_CODES:
                return True
    except ImportError:
        pass

    # Check for httpx exception types if httpx is present
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
            return True
        if (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code in _TRANSIENT_STATUS_CODES
        ):
            return True
    except ImportError:
        pass

    return False


def _is_gone(exc: Exception) -> bool:
    """True when a resource is permanently gone or not found (403, 404)."""
    if isinstance(exc, PostmanNotFoundError):
        return True
    if isinstance(exc, KeyError) and not isinstance(exc, IndexError | TypeError):
        return True

    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "status", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
        or getattr(exc, "code", None)
    )
    return status in (403, 404)


def _extract_error_detail(response: Any) -> str:
    """Extract human-readable error description from Postman error response."""
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                name = err.get("name", "")
                msg = err.get("message", "")
                return f"{name}: {msg}".strip(": ")
            if isinstance(err, str):
                return err
            if "message" in data:
                return str(data["message"])
    except Exception:
        pass
    text = getattr(response, "text", "")
    return text[:200] if text else f"HTTP status {getattr(response, 'status_code', 'unknown')}"


def _request(method: Any, **kwargs: Any) -> Any:
    """Call a Postman API method or callable, retrying rate-limit / transient errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            return method(**kwargs)
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            headers = getattr(exc, "headers", None)
            if headers is None:
                headers = getattr(getattr(exc, "response", None), "headers", None)
            delay = _retry_after(headers, attempt)
            logger.warning(
                "Postman: %s: retrying in %.1fs (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)


class PostmanClient:
    """HTTP client for Postman REST API v10.

    Handles authentication via X-Api-Key, request execution, endpoint mapping,
    rate limit backoff (HTTP 429), and transient error retries.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.getpostman.com",
        timeout: float = 30.0,
        session: Any | None = None,
        client: Any | None = None,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        """Initialize PostmanClient with API key and configuration.

        Args:
            api_key: Postman API key. Falls back to POSTMAN_API_KEY environment variable.
            base_url: Postman API base URL (defaults to https://api.getpostman.com).
            timeout: Request timeout in seconds (defaults to 30.0).
            session: Optional custom HTTP session for testing or custom transports.
            client: Alias for session.
            max_retries: Maximum retry attempts for transient errors and rate limits.

        Raises:
            ValueError: If API key is missing or blank.
        """
        resolved_key = api_key or os.environ.get("POSTMAN_API_KEY")
        if not resolved_key or not resolved_key.strip():
            raise ValueError(
                "Postman API key required: pass api_key= or "
                "set POSTMAN_API_KEY environment variable."
            )
        self.api_key: str = resolved_key.strip()
        self.base_url: str = base_url.rstrip("/")
        self.timeout: float = float(timeout)
        self.max_retries: int = int(max_retries)

        self._default_headers: dict[str, str] = {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-postman/0.1.0",
        }

        # Initialize or assign session
        active_session = session or client
        if active_session is None:
            try:
                import requests

                active_session = requests.Session()
                active_session.headers.update(self._default_headers)
            except ImportError:
                try:
                    import httpx

                    active_session = httpx.Client(
                        headers=self._default_headers,
                        timeout=self.timeout,
                    )
                except ImportError as exc:
                    raise ImportError(
                        "The Postman connector requires requests or httpx. "
                        "Install with: pip install requests"
                    ) from exc
        else:
            if hasattr(active_session, "headers") and hasattr(active_session.headers, "update"):
                active_session.headers.update(self._default_headers)

        self._session: Any = active_session
        logger.debug(
            "Initialized PostmanClient with base_url=%s, timeout=%.1fs, max_retries=%d",
            self.base_url,
            self.timeout,
            self.max_retries,
        )

    def close(self) -> None:
        """Close the underlying HTTP session if supported."""
        if hasattr(self._session, "close") and callable(self._session.close):
            self._session.close()

    def __enter__(self) -> PostmanClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute HTTP request against Postman API with resilience and retries.

        Handles:
            - Injection of X-Api-Key header.
            - HTTP 429 with Retry-After header parsing and exponential backoff.
            - HTTP 500, 502, 503, 504 with exponential backoff.
            - HTTP 401, 403 raising PermissionError subclasses.
            - HTTP 404 raising PostmanNotFoundError (KeyError subclass).
        """
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        headers = dict(self._default_headers)
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))

        timeout = kwargs.pop("timeout", self.timeout)

        for attempt in range(self.max_retries):
            try:
                logger.debug(
                    "Postman API request: %s %s (params=%s, attempt=%d/%d)",
                    method,
                    url,
                    params,
                    attempt + 1,
                    self.max_retries,
                )
                if hasattr(self._session, "request"):
                    response = self._session.request(
                        method=method,
                        url=url,
                        params=params,
                        headers=headers,
                        timeout=timeout,
                        **kwargs,
                    )
                else:
                    # Generic session fallback
                    call_func = getattr(self._session, method.lower())
                    response = call_func(
                        url,
                        params=params,
                        headers=headers,
                        timeout=timeout,
                        **kwargs,
                    )
            except Exception as exc:
                if attempt == self.max_retries - 1 or not _is_transient(exc):
                    logger.error(
                        "Postman API request failed after %d attempts: %s %s (%s)",
                        attempt + 1,
                        method,
                        url,
                        exc,
                    )
                    raise
                headers_obj = getattr(exc, "headers", None)
                if headers_obj is None:
                    headers_obj = getattr(getattr(exc, "response", None), "headers", None)
                delay = _retry_after(headers_obj, attempt)
                logger.warning(
                    "Postman API network error: %s. Retrying in %.2fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)
                continue

            status_code = getattr(response, "status_code", 200)

            # Success
            if 200 <= status_code < 300:
                logger.debug("Postman API response success: %d", status_code)
                try:
                    return response.json()
                except Exception as json_err:
                    raise PostmanAPIError(
                        f"Failed to decode Postman JSON response: {json_err}",
                        status_code=status_code,
                    ) from json_err

            # Rate limit handling (HTTP 429)
            if status_code == 429:
                detail = _extract_error_detail(response)
                resp_headers = getattr(response, "headers", None)
                delay = _retry_after(resp_headers, attempt)
                if attempt == self.max_retries - 1:
                    logger.error("Postman API rate limit exhausted: %s", detail)
                    msg = (
                        f"Postman API rate limit exceeded after {self.max_retries} attempts: "
                        f"{detail}"
                    )
                    raise PostmanRateLimitError(
                        msg,
                        status_code=429,
                        retry_after=delay,
                        response_body=detail,
                    )
                logger.warning(
                    "Postman API rate limit hit (429). Retrying in %.2fs (attempt %d/%d).",
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)
                continue

            # Transient server errors (5xx)
            if status_code in (500, 502, 503, 504):
                detail = _extract_error_detail(response)
                if attempt == self.max_retries - 1:
                    logger.error(
                        "Postman API server error exhausted: %d: %s",
                        status_code,
                        detail,
                    )
                    raise PostmanAPIError(
                        f"Postman API server error ({status_code}): {detail}",
                        status_code=status_code,
                        response_body=detail,
                    )
                resp_headers = getattr(response, "headers", None)
                delay = _retry_after(resp_headers, attempt)
                logger.warning(
                    "Postman API transient server error (%d). Retrying in %.2fs (attempt %d/%d).",
                    status_code,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)
                continue

            # Authentication / Authorization errors (401, 403)
            detail = _extract_error_detail(response)
            if status_code == 401:
                logger.error("Postman API authentication failed (401): %s", detail)
                raise PostmanAuthenticationError(
                    f"Postman authentication failed (HTTP 401): {detail}",
                    status_code=401,
                    response_body=detail,
                )
            if status_code == 403:
                logger.error("Postman API access forbidden (403): %s", detail)
                raise PostmanPermissionError(
                    f"Postman access forbidden (HTTP 403): {detail}",
                    status_code=403,
                    response_body=detail,
                )

            # Not Found error (404)
            if status_code == 404:
                logger.warning("Postman API resource not found (404): %s", detail)
                raise PostmanNotFoundError(
                    f"Postman resource not found (HTTP 404): {detail}",
                    status_code=404,
                    response_body=detail,
                )

            # Other client errors (e.g. 400 Bad Request)
            logger.error("Postman API client error (%d): %s", status_code, detail)
            raise PostmanAPIError(
                f"Postman API error ({status_code}): {detail}",
                status_code=status_code,
                response_body=detail,
            )

        raise PostmanAPIError(f"Postman request failed after {self.max_retries} attempts.")

    # Alias _execute_request to _request for flexible caller compatibility
    _execute_request = _request

    def get_collections(
        self,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch list of accessible Postman collections.

        Args:
            workspace_id: Optional Postman workspace ID filter.

        Returns:
            List of collection summary dictionaries containing id, uid, name,
            createdAt, updatedAt, owner, and isPublic.
        """
        params = {"workspace": workspace_id} if workspace_id else None
        data = self._request("GET", "/collections", params=params)
        collections = data.get("collections", []) if isinstance(data, dict) else []
        logger.info("Retrieved %d Postman collection(s).", len(collections))
        return collections

    def get_collection(
        self,
        collection_uid: str,
    ) -> dict[str, Any]:
        """Fetch full collection JSON schema for the given collection UID.

        Args:
            collection_uid: Unique identifier for the collection (owner-id or id).

        Returns:
            Collection root dictionary containing info, item hierarchy, and variables.

        Raises:
            ValueError: If collection_uid is empty.
            PostmanNotFoundError: If collection does not exist (HTTP 404).
            PermissionError: If unauthorized or forbidden (HTTP 401/403).
        """
        if not collection_uid or not str(collection_uid).strip():
            raise ValueError("collection_uid cannot be empty.")

        safe_uid = urllib.parse.quote(str(collection_uid).strip(), safe="")
        data = self._request("GET", f"/collections/{safe_uid}")
        collection = data.get("collection", data) if isinstance(data, dict) else {}
        is_missing = (
            not collection
            and isinstance(data, dict)
            and "collection" in data
            and data["collection"] is None
        )
        if is_missing:
            raise PostmanNotFoundError(
                f"Collection '{collection_uid}' not found in Postman API response",
                status_code=404,
            )
        info = collection.get("info", {}) if isinstance(collection, dict) else {}
        name = info.get("name", "unnamed")
        logger.info(
            "Retrieved Postman collection details for uid=%s (name='%s').",
            collection_uid,
            name,
        )
        return collection
