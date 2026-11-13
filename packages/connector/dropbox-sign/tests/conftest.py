"""In-memory stand-in for the Dropbox Sign API v3, shared by the test modules.

Answers follow the shapes in Dropbox Sign's OpenAPI spec: ``/account`` names the
account, and ``/signature_request/list`` pages newest first with ``list_info``.
Requests carry form values, custom fields, metadata, URLs and phone numbers with
planted secrets, which must never reach a document.
"""

import pytest

from cognee_community_connector_dropbox_sign.dropbox_sign import (
    DropboxSignAPIError,
    DropboxSignAuthError,
    DropboxSignRateLimitedError,
)

NOW = 1_793_000_000  # 2026-10-26, as a Unix timestamp
DAY = 86_400
SECRET_FORM = "form-value-secret-1a2b"
SECRET_FIELD = "custom-field-secret-3c4d"
SECRET_META = "metadata-secret-5e6f"
SECRET_PHONE = "+15550100777"
SECRET_URL = "https://app.hellosign.com/sign/secret-signing-link"


class FakeDropboxSign:
    """Implements ``get`` like Dropbox Sign. ``page_size`` overrides the page length."""

    def __init__(self, account_id="acct-1", page_size=None):
        self.account_id = account_id
        self.page_size = page_size
        self.requests: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, int] = {}
        self.rate_limited: set[str] = set()
        self.bad_key = False
        self.unordered = False
        self.missing_list = False
        self._next = 0

    def add(self, title="NDA", *, created=None, signers=None, test_mode=False, message=""):
        """Send a request; signers are (name, email) pairs, all awaiting signature."""
        self._next += 1
        request_id = f"sr{self._next:04d}"
        signers = signers if signers is not None else [("Priya Shah", "priya@example.com")]
        self.requests[request_id] = {
            "signature_request_id": request_id,
            "test_mode": test_mode,
            "title": title,
            "original_title": title,
            "subject": f"Please sign: {title}",
            "message": message,
            "requester_email_address": "sender@example.com",
            "created_at": created if created is not None else NOW - 3600 + self._next,
            "expires_at": None,
            "is_complete": False,
            "is_declined": False,
            "has_error": False,
            "details_url": f"https://app.hellosign.com/home/manage?guid={request_id}",
            "files_url": f"https://api.hellosign.com/v3/signature_request/files/{request_id}",
            "signing_url": SECRET_URL,
            "final_copy_uri": f"/v3/signature_request/final_copy/{request_id}",
            "cc_email_addresses": ["cc-person@example.com"],
            "metadata": {"internal": SECRET_META},
            "custom_fields": [{"name": "Salary", "type": "text", "value": SECRET_FIELD}],
            "response_data": [{"api_id": "f1", "name": "Bank", "value": SECRET_FORM}],
            "attachments": [{"id": "a1", "signer": "1", "name": "id-scan"}],
            "signatures": [
                {
                    "signature_id": f"{request_id}-s{n}",
                    "signer_email_address": email,
                    "signer_name": name,
                    "signer_role": None,
                    "order": None,
                    "status_code": "awaiting_signature",
                    "signed_at": None,
                    "last_viewed_at": None,
                    "last_reminded_at": None,
                    "sms_phone_number": SECRET_PHONE,
                    "decline_reason": None,
                }
                for n, (name, email) in enumerate(signers)
            ],
        }
        return request_id

    def sign(self, request_id, signer=0, at=NOW - 60):
        request = self.requests[request_id]
        signature = request["signatures"][signer]
        signature.update(status_code="signed", signed_at=at)
        request["is_complete"] = all(s["status_code"] == "signed" for s in request["signatures"])

    def decline(self, request_id, signer=0, reason="Wrong rate"):
        request = self.requests[request_id]
        request["signatures"][signer].update(status_code="declined", decline_reason=reason)
        request["is_declined"] = True

    def cancel(self, request_id):
        self.requests.pop(request_id)

    def _fail(self, path):
        if self.bad_key:
            raise DropboxSignAuthError("Dropbox Sign rejected the API key (HTTP 401)")
        if any(key in path for key in self.rate_limited):
            raise DropboxSignRateLimitedError("Dropbox Sign rate limit exceeded (HTTP 429)")
        for key, status in self.errors.items():
            if key in path:
                if status == 401:
                    raise DropboxSignAuthError("Dropbox Sign rejected the API key (HTTP 401)")
                raise DropboxSignAPIError(f"Dropbox Sign request failed: HTTP {status}", status)

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        self._fail(path)
        if path == "/account":
            return {"account": {"account_id": self.account_id, "email_address": "me@example.com"}}
        if path != "/signature_request/list":
            raise AssertionError(f"unexpected path {path}")
        if self.missing_list:
            return {"list_info": {"num_pages": 1, "page": 1}}
        items = list(self.requests.values())
        if "query" in params:
            needle = params["query"].removeprefix("title:")
            items = [r for r in items if needle in r["title"]]
        items.sort(key=lambda r: r["created_at"], reverse=not self.unordered)
        size = self.page_size or int(params.get("page_size", 20))
        assert size <= 100, "page_size must be between 1 and 100"
        page = int(params.get("page", 1))
        pages = max(1, -(-len(items) // size))
        chunk = items[(page - 1) * size : page * size]
        return {
            "signature_requests": [dict(r) for r in chunk],
            "list_info": {
                "num_pages": pages,
                "num_results": len(items),
                "page": page,
                "page_size": size,
            },
        }

    def paths(self):
        return [p for p, _ in self.calls]


@pytest.fixture
def sign():
    return FakeDropboxSign()
