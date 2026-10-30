"""In-memory stand-ins for a GA4 property: one at the client level, one at the discovery level."""

import json

from googleapiclient.errors import HttpError
from httplib2 import Response

from cognee_community_connector_ga4.ga4 import _COLLECTIONS

PROPERTY = "123456789"


def _name(kind: str, record_id: object) -> str:
    return f"properties/{PROPERTY}/{_COLLECTIONS[kind][1]}/{record_id}"


class FakeGA4:
    """Answers ``list(kind, property_id)`` like GA4Client, from records seeded per kind."""

    def __init__(self):
        self.records: dict[str, dict[str, dict]] = {kind: {} for kind in _COLLECTIONS}
        # canned answers returned instead of the real listing, to fake a broken response
        self.overrides: dict[str, object] = {}
        self.fail_kinds: dict[str, Exception] = {}
        self.calls: list[tuple[str, str]] = []

    def list(self, kind: str, property_id: str) -> list[dict]:
        self.calls.append((kind, property_id))
        if kind in self.fail_kinds:
            raise self.fail_kinds[kind]
        if kind in self.overrides:
            return self.overrides[kind]
        return [dict(record) for record in self.records[kind].values()]

    # -- seeding -----------------------------------------------------------
    def _add(self, kind: str, record_id: object, **fields) -> dict:
        record = {"name": _name(kind, record_id), **fields}
        self.records[kind][str(record_id)] = record
        return record

    def add_custom_dimension(self, record_id, parameter, display_name, **fields) -> dict:
        return self._add(
            "custom_dimensions",
            record_id,
            parameterName=parameter,
            displayName=display_name,
            scope=fields.pop("scope", "EVENT"),
            **fields,
        )

    def add_custom_metric(self, record_id, parameter, display_name, **fields) -> dict:
        return self._add(
            "custom_metrics",
            record_id,
            parameterName=parameter,
            displayName=display_name,
            scope="EVENT",
            measurementUnit=fields.pop("measurementUnit", "STANDARD"),
            **fields,
        )

    def add_key_event(self, record_id, event_name, **fields) -> dict:
        return self._add(
            "key_events",
            record_id,
            eventName=event_name,
            createTime="2026-10-01T09:00:00.000000Z",
            countingMethod=fields.pop("countingMethod", "ONCE_PER_EVENT"),
            custom=fields.pop("custom", True),
            deletable=True,
            **fields,
        )

    def add_audience(self, record_id, display_name, description, **fields) -> dict:
        clause = {
            "clauseType": "INCLUDE",
            "simpleFilter": {
                "scope": "AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS",
                "filterExpression": {
                    "andGroup": {
                        "filterExpressions": [
                            {
                                "orGroup": {
                                    "filterExpressions": [
                                        {"eventFilter": {"eventName": "purchase"}}
                                    ]
                                }
                            }
                        ]
                    }
                },
            },
        }
        return self._add(
            "audiences",
            record_id,
            displayName=display_name,
            description=description,
            membershipDurationDays=fields.pop("membershipDurationDays", 30),
            adsPersonalizationEnabled=True,
            exclusionDurationMode="EXCLUDE_TEMPORARILY",
            filterClauses=fields.pop("filterClauses", [clause]),
            createTime="2026-10-01T09:00:00.000000Z",
            **fields,
        )

    def add_annotation(self, record_id, title, **fields) -> dict:
        return self._add(
            "annotations",
            record_id,
            title=title,
            color="BLUE",
            annotationDate=fields.pop("annotationDate", {"year": 2026, "month": 10, "day": 1}),
            **fields,
        )


# ---------------------------------------------------------------------------
# discovery-level fake, for the real GA4Client
# ---------------------------------------------------------------------------
def http_error(status: int, google_status: str = "", reason: str = "") -> HttpError:
    error = {"code": status, "message": "body text that must not leak", "status": google_status}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return HttpError(Response({"status": status}), json.dumps({"error": error}).encode())


class _Request:
    def __init__(self, service: "FakeAdminService", collection: str, params: dict):
        self.service = service
        self.collection = collection
        self.params = params

    def execute(self, num_retries=0):
        self.service.executed.append((self.collection, dict(self.params), num_retries))
        if self.service.failures:
            raise self.service.failures.pop(0)
        return self.service.pages[self.collection][self.params.get("pageToken")]


class _Collection:
    def __init__(self, service: "FakeAdminService", collection: str):
        self.service = service
        self.collection = collection

    def list(self, **params):
        return _Request(self.service, self.collection, params)


class _Properties:
    def __init__(self, service: "FakeAdminService"):
        self.service = service

    def customDimensions(self):
        return _Collection(self.service, "customDimensions")

    def customMetrics(self):
        return _Collection(self.service, "customMetrics")

    def keyEvents(self):
        return _Collection(self.service, "keyEvents")

    def audiences(self):
        return _Collection(self.service, "audiences")

    def reportingDataAnnotations(self):
        return _Collection(self.service, "reportingDataAnnotations")


class FakeAdminService:
    """The slice of a discovery-built analyticsadmin client that GA4Client calls.

    ``pages`` maps a collection to its pages, keyed by the page token that fetches them.
    """

    def __init__(self):
        self.pages: dict[str, dict[str | None, object]] = {}
        self.failures: list[Exception] = []
        self.executed: list[tuple[str, dict, int]] = []

    def properties(self):
        return _Properties(self)
