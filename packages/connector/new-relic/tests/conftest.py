"""Fixtures and mock New Relic NerdGraph server using httpx.MockTransport."""

import json
from typing import Any

import httpx
import pytest


class FakeNewRelicServer:
    """Mock in-memory New Relic NerdGraph GraphQL API server."""

    def __init__(self) -> None:
        self.valid_api_key = "NRAK-VALID-TEST-KEY"
        self.account_id = 1234567
        self.rate_limit_hits = 0
        self.server_error_hits = 0
        self.return_partial_errors = False
        self.return_fatal_errors = False

        self.policies: list[dict[str, Any]] = [
            {"id": "pol-101", "name": "Production Latency SLA", "incidentPreference": "PER_POLICY"},
            {
                "id": "pol-102",
                "name": "Database Error Rates",
                "incidentPreference": "PER_CONDITION",
            },
        ]

        self.conditions: list[dict[str, Any]] = [
            {
                "id": "cond-1",
                "name": "High P99 Web Response Time",
                "policyId": "pol-101",
                "enabled": True,
                "description": "Trigger when P99 latency exceeds 500ms for 5 minutes.",
                "nrql": {
                    "query": (
                        "SELECT percentile(duration, 99) FROM Transaction WHERE appName = 'Web'"
                    )
                },
                "runbookUrl": "https://wiki.internal/runbooks/high-p99",
                "terms": [
                    {
                        "priority": "CRITICAL",
                        "operator": "ABOVE",
                        "threshold": 0.5,
                        "thresholdDuration": 300,
                    }
                ],
            },
            {
                "id": "cond-2",
                "name": "Postgres Connection Spikes",
                "policyId": "pol-102",
                "enabled": True,
                "description": "Alert when connections exceed 90% pool capacity.",
                "nrql": {"query": "SELECT max(activeConnections) FROM DatabaseSample"},
                "runbookUrl": None,
                "terms": [
                    {
                        "priority": "WARNING",
                        "operator": "ABOVE",
                        "threshold": 90,
                        "thresholdDuration": 180,
                    }
                ],
            },
        ]

        self.dashboards: list[dict[str, Any]] = [
            {
                "guid": "guid-dash-1",
                "name": "Core Services Overview",
                "description": "Real-time health of authentication and billing services.",
                "accountId": 1234567,
                "updatedAt": "2026-10-20T14:00:00Z",
                "tags": [{"key": "Environment", "values": ["Production"]}],
                "pages": [
                    {
                        "name": "Services Summary",
                        "widgets": [
                            {
                                "title": "HTTP Error Rate",
                                "rawConfiguration": {
                                    "nrqlQueries": [
                                        {
                                            "query": (
                                                "SELECT percentage(count(*), WHERE error is true) "
                                                "FROM Transaction"
                                            )
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]

        self.issues: list[dict[str, Any]] = [
            {
                "issueId": "iss-501",
                "title": "High P99 Web Response Time Triggered",
                "priority": "CRITICAL",
                "state": "ACTIVATED",
                "activatedAt": "2026-10-25T10:00:00Z",
                "closedAt": None,
                "sources": ["Alert Condition: High P99"],
                "mutingState": "NOT_MUTED",
                "conditionName": "High P99 Web Response Time",
            }
        ]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        auth_key = request.headers.get("API-Key", "")
        if auth_key != self.valid_api_key:
            return httpx.Response(401, json={"message": "Unauthorized: Invalid API-Key"})

        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return httpx.Response(429, json={"message": "Rate limit exceeded"})

        if self.server_error_hits > 0:
            self.server_error_hits -= 1
            return httpx.Response(503, json={"message": "Service Temporarily Unavailable"})

        if self.return_fatal_errors:
            return httpx.Response(
                200,
                json={"data": None, "errors": [{"message": "Fatal NerdGraph query parsing error"}]},
            )

        body = json.loads(request.content.decode("utf-8"))
        query = body.get("query", "")
        variables = body.get("variables", {}) or {}

        errors_payload = (
            [{"message": "Warning: Field is scheduled for deprecation."}]
            if self.return_partial_errors
            else None
        )

        if "GetAlertPolicies" in query:
            cursor = variables.get("cursor")
            # Simple cursor simulation
            slice_policies = self.policies if not cursor else []
            resp_data = {
                "data": {
                    "actor": {
                        "account": {
                            "alerts": {
                                "policiesSearch": {
                                    "policies": slice_policies,
                                    "nextCursor": None,
                                },
                                "nrqlConditionsSearch": {
                                    "nrqlConditions": self.conditions,
                                },
                            }
                        }
                    }
                }
            }
            if errors_payload:
                resp_data["errors"] = errors_payload
            return httpx.Response(200, json=resp_data)

        if "GetDashboards" in query:
            resp_data = {
                "data": {
                    "actor": {
                        "entitySearch": {
                            "results": {
                                "entities": self.dashboards,
                                "nextCursor": None,
                            }
                        }
                    }
                }
            }
            if errors_payload:
                resp_data["errors"] = errors_payload
            return httpx.Response(200, json=resp_data)

        if "GetIssues" in query:
            resp_data = {
                "data": {
                    "actor": {
                        "account": {
                            "aiIssues": {
                                "issues": {
                                    "issues": self.issues,
                                    "nextCursor": None,
                                }
                            }
                        }
                    }
                }
            }
            if errors_payload:
                resp_data["errors"] = errors_payload
            return httpx.Response(200, json=resp_data)

        return httpx.Response(404, json={"message": f"Unhandled query pattern: {query[:50]}"})


@pytest.fixture
def fake_nr():
    return FakeNewRelicServer()


@pytest.fixture
def nr_client(fake_nr):
    transport = httpx.MockTransport(fake_nr.handle_request)
    return httpx.Client(
        headers={"API-Key": fake_nr.valid_api_key, "Content-Type": "application/json"},
        transport=transport,
    )
