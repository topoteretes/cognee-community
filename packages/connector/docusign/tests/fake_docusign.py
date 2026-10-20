"""In-memory DocuSign stand-in for the connector tests.

``FakeDocuSign`` is mounted on a real ``requests.Session`` as a transport
adapter, so the connector's whole HTTP stack runs unchanged: the JWT exchange,
``/oauth/userinfo`` account discovery, ``nextUri`` paging, retries and token
renewal. Only the network is replaced.

It mirrors the DocuSign behaviour the connector relies on:

* ``/envelopes`` needs ``from_date`` unless ``envelope_ids`` is given, filters
  on ``statusChangedDateTime`` (``from_to_status=changed``), ``status`` and
  ``folder_ids``, and pages through ``nextUri`` with string-typed counters;
* moving an envelope to the Deleted folder, or purging it, does **not** change
  ``statusChangedDateTime`` (this is why the connector rechecks known ids);
* a purge with metadata removes the envelope from every listing;
* ``documents/combined`` returns a real PDF whose text layer ``pypdf`` reads.
"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import jwt
import requests
from requests.adapters import BaseAdapter

OAUTH_HOST = "account-d.docusign.com"
API_HOST = "demo.docusign.net"
ACCOUNT_ID = "acct-1"
USER_ID = "user-guid-1"
INTEGRATION_KEY = "integration-key-1"
API_ROOT = f"/restapi/v2.1/accounts/{ACCOUNT_ID}"

RECYCLE_BIN = {"folderId": "recyclebin", "name": "Deleted Items", "type": "recyclebin"}
SENT_ITEMS = {"folderId": "sentitems", "name": "Sent Items", "type": "sentitems"}


def make_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF whose text layer is ``lines`` (empty: a "scan")."""
    stream = "BT /F1 12 Tf 14 TL 40 760 Td\n"
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream += f"({escaped}) Tj T*\n"
    stream += "ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


class FakeDocuSign(BaseAdapter):
    """Serves the handful of eSignature endpoints the connector reads."""

    def __init__(self, public_key: str | None = None, page_size_cap: int = 100):
        super().__init__()
        self.public_key = public_key
        self.page_size_cap = page_size_cap
        self.envelopes: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.token_requests = 0
        self.consent_required = False
        self.token_lifetime = 3600
        self.valid_tokens: set[str] = set()
        self.static_token: str | None = None
        self.accounts = [
            {
                "account_id": ACCOUNT_ID,
                "is_default": "true",
                "base_uri": f"https://{API_HOST}",
            }
        ]
        # (path substring, status, headers): served once instead of the real response.
        self.failures: list[tuple[str, int, dict]] = []
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)
        self._next_id = 100

    # -- fixture builders --------------------------------------------------

    def session(self) -> requests.Session:
        session = requests.Session()
        session.mount(f"https://{OAUTH_HOST}/", self)
        for account in self.accounts:
            session.mount(account["base_uri"] + "/", self)
        return session

    def add_envelope(
        self,
        subject: str,
        text: str = "",
        *,
        status: str = "completed",
        signers: list[dict] | None = None,
        custom: dict[str, str] | None = None,
        folders: list[dict] | None = None,
        form: dict[str, str] | None = None,
        pdf: bytes | None = None,
    ) -> str:
        self._next_id += 1
        envelope_id = f"env-{self._next_id}"
        now = self._tick()
        signers = signers if signers is not None else [{"name": "Alice Smith"}]
        self.envelopes[envelope_id] = {
            "envelopeId": envelope_id,
            "status": status,
            "emailSubject": subject,
            "sender": {"userName": "Sam Sender", "email": "sam@example.com", "userId": "u-sam"},
            "sentDateTime": now,
            "completedDateTime": now if status == "completed" else None,
            "createdDateTime": now,
            "statusChangedDateTime": now,
            "purgeState": "unpurged",
            "folders": list(folders if folders is not None else [SENT_ITEMS]),
            "customFields": {
                "textCustomFields": [{"name": k, "value": v} for k, v in (custom or {}).items()],
                "listCustomFields": [],
            },
            "recipients": {
                "signers": [self._signer(i, s, status, now) for i, s in enumerate(signers, 1)],
                "carbonCopies": [],
            },
            "_pdf": pdf if pdf is not None else make_pdf(text.splitlines() or [subject]),
            "_form": form or {},
            "_metadata_purged": False,
        }
        return envelope_id

    def complete(self, envelope_id: str) -> None:
        envelope = self.envelopes[envelope_id]
        now = self._tick()
        envelope["status"] = "completed"
        envelope["completedDateTime"] = now
        envelope["statusChangedDateTime"] = now
        for signer in envelope["recipients"]["signers"]:
            signer["status"] = "completed"
            signer["signedDateTime"] = now

    def void(self, envelope_id: str) -> None:
        envelope = self.envelopes[envelope_id]
        envelope["status"] = "voided"
        envelope["statusChangedDateTime"] = self._tick()

    def change_status(self, envelope_id: str, status: str) -> None:
        envelope = self.envelopes[envelope_id]
        envelope["status"] = status
        envelope["statusChangedDateTime"] = self._tick()

    def edit_text(self, envelope_id: str, text: str) -> None:
        """A status change (e.g. a correction) that also changes the document text."""
        envelope = self.envelopes[envelope_id]
        envelope["_pdf"] = make_pdf(text.splitlines())
        envelope["statusChangedDateTime"] = self._tick()

    def move_to_recycle_bin(self, envelope_id: str) -> None:
        """Does NOT touch statusChangedDateTime, like the real thing."""
        self.envelopes[envelope_id]["folders"] = [dict(RECYCLE_BIN)]

    def move_to_folder(self, envelope_id: str, folder: dict) -> None:
        self.envelopes[envelope_id]["folders"] = [dict(folder)]

    def purge(self, envelope_id: str, *, metadata: bool = False) -> None:
        """Does NOT touch statusChangedDateTime. ``metadata`` removes it from listings."""
        envelope = self.envelopes[envelope_id]
        envelope["purgeState"] = "documents_and_metadata_purged" if metadata else "documents_purged"
        envelope["_metadata_purged"] = metadata
        envelope["_pdf"] = b""

    def fail_once(self, path_fragment: str, status: int, headers: dict | None = None) -> None:
        self.failures.append((path_fragment, status, headers or {}))

    def requested(self, fragment: str) -> list[str]:
        return [path for _method, path, _query in self.requests if fragment in path]

    def listing_queries(self) -> list[dict]:
        return [q for _m, path, q in self.requests if path.endswith("/envelopes")]

    # -- transport ---------------------------------------------------------

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if url.hostname == OAUTH_HOST:
            return self._oauth(request, url.path)

        assert request.method == "GET", "the connector must be read-only"
        path = unquote(url.path)
        self.requests.append((request.method, path, query))

        for index, (fragment, status, headers) in enumerate(self.failures):
            if fragment in path:
                del self.failures[index]
                return self._json(request, status, {"errorCode": "FAKE", "message": "x"}, headers)

        if not self._authorized(request.headers.get("Authorization", "")):
            return self._json(request, 401, {"errorCode": "AUTHORIZATION_INVALID_TOKEN"})

        if path == f"{API_ROOT}/envelopes":
            return self._list(request, query)
        parts = path.removeprefix(API_ROOT).strip("/").split("/")
        if len(parts) == 3 and parts[0] == "envelopes" and parts[2] == "form_data":
            return self._form_data(request, parts[1])
        if parts[0] == "envelopes" and parts[2:] == ["documents", "combined"]:
            return self._combined(request, parts[1], query)
        return self._json(request, 404, {"errorCode": "NOT_FOUND"})

    def close(self):
        pass

    # -- endpoints ---------------------------------------------------------

    def _oauth(self, request, path):
        if path == "/oauth/token":
            self.token_requests += 1
            body = parse_qs(request.body if isinstance(request.body, str) else "")
            assert body["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            if self.consent_required:
                return self._json(request, 400, {"error": "consent_required"})
            claims = jwt.decode(
                body["assertion"][0],
                self.public_key,
                algorithms=["RS256"],
                audience=OAUTH_HOST,
                # tests move the connector's clock; the lifetime is asserted below
                options={"verify_exp": False, "verify_iat": False},
            )
            assert claims["exp"] - claims["iat"] == 3600
            assert claims["iss"] == INTEGRATION_KEY
            assert claims["sub"] == USER_ID
            assert claims["scope"] == "signature impersonation"
            token = f"token-{self.token_requests}"
            self.valid_tokens.add(token)
            return self._json(
                request,
                200,
                {"access_token": token, "token_type": "Bearer", "expires_in": self.token_lifetime},
            )
        if path == "/oauth/userinfo":
            if not self._authorized(request.headers.get("Authorization", "")):
                return self._json(request, 401, {"error": "invalid_token"})
            return self._json(request, 200, {"sub": USER_ID, "accounts": self.accounts})
        return self._json(request, 404, {})

    def _authorized(self, header: str) -> bool:
        token = header.removeprefix("Bearer ")
        return token in self.valid_tokens or (
            self.static_token is not None and token == self.static_token
        )

    def _list(self, request, query):
        ids = query.get("envelope_ids")
        if ids is None and "from_date" not in query:
            return self._json(request, 400, {"errorCode": "MISSING_FROM_DATE", "message": "x"})
        top = int(query.get("count", 100))
        assert top <= self.page_size_cap
        start = int(query.get("start_position", 0))

        selected = [e for e in self.envelopes.values() if not e["_metadata_purged"]]
        if ids is not None:
            wanted = set(ids.split(","))
            selected = [e for e in selected if e["envelopeId"] in wanted]
        else:
            assert query.get("from_to_status") == "changed"
            floor = _parse(query["from_date"])
            selected = [e for e in selected if _parse(e["statusChangedDateTime"]) >= floor]
            if "status" in query:
                selected = [e for e in selected if e["status"] in query["status"].split(",")]
            if "folder_ids" in query:
                folder_ids = set(query["folder_ids"].split(","))
                selected = [
                    e
                    for e in selected
                    if folder_ids & {f[key] for f in e["folders"] for key in ("folderId", "name")}
                ]
        selected.sort(key=lambda e: (e["statusChangedDateTime"], e["envelopeId"]))
        page = selected[start : start + top]
        includes = set(query.get("include", "").split(","))
        body: dict = {
            "resultSetSize": str(len(page)),
            "totalSetSize": str(len(selected)),
            "startPosition": str(start),
            "endPosition": str(start + max(len(page) - 1, 0)),
        }
        if page:
            body["envelopes"] = [self._view(e, includes, query) for e in page]
        if start + top < len(selected):
            rest = {k: v for k, v in query.items() if k != "start_position"}
            rest["start_position"] = str(start + top)
            qs = "&".join(f"{k}={v}" for k, v in rest.items())
            body["nextUri"] = f"{API_ROOT}/envelopes?{qs}"
        return self._json(request, 200, body)

    def _view(self, envelope, includes, query):
        view = {k: v for k, v in envelope.items() if not k.startswith("_")}
        if "recipients" not in includes:
            view.pop("recipients")
        if "custom_fields" not in includes:
            view.pop("customFields")
        if "folders" not in includes:
            view.pop("folders")
        if query.get("include_purge_information") != "true":
            view.pop("purgeState")
        return view

    def _form_data(self, request, envelope_id):
        envelope = self.envelopes.get(envelope_id)
        if envelope is None:
            return self._json(request, 404, {"errorCode": "NOT_FOUND"})
        signer = envelope["recipients"]["signers"][0]
        fields = [{"name": k, "value": v} for k, v in envelope["_form"].items()]
        body = {
            "envelopeId": envelope_id,
            "formData": fields,
            "recipientFormData": [
                {"name": signer["name"], "email": signer["email"], "formData": fields}
            ]
            if fields
            else [],
        }
        return self._json(request, 200, body)

    def _combined(self, request, envelope_id, query):
        envelope = self.envelopes.get(envelope_id)
        if envelope is None or envelope["_metadata_purged"]:
            return self._json(request, 404, {"errorCode": "ENVELOPE_DOES_NOT_EXIST"})
        assert query.get("certificate") in ("true", "false")
        response = requests.Response()
        response.status_code = 200
        response._content = envelope["_pdf"]
        response.headers["Content-Type"] = "application/pdf"
        response.url = request.url
        response.request = request
        return response

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _signer(order, signer, status, now):
        name = signer["name"]
        done = status == "completed"
        return {
            "name": name,
            "email": signer.get("email", f"{name.split()[0].lower()}@example.com"),
            "roleName": signer.get("role", ""),
            "routingOrder": str(order),
            "status": "completed" if done else "sent",
            "signedDateTime": now if done else None,
        }

    def _tick(self) -> str:
        # Well past the connector's cursor overlap, so every change is
        # unambiguously newer than the previous sync. DocuSign sends 7 digits.
        self._clock += timedelta(hours=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%S.") + "1234567Z"

    @staticmethod
    def _json(request, status, payload, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        response.headers.update(headers or {})
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response


def _parse(value: str) -> datetime:
    text = value.rstrip("Z")
    head, _, fraction = text.partition(".")
    return datetime.fromisoformat(f"{head}.{(fraction + '000000')[:6]}").replace(tzinfo=UTC)
