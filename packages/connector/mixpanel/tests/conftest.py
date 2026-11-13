"""Synthetic fixtures, fake client and isolated offline configuration.

Captured responses from the dedicated synthetic tenant live in fixtures/.
"""

import copy
import os

os.environ.setdefault("COGNEE_TELEMETRY_DISABLED", "1")
os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

import pytest


@pytest.fixture(autouse=True)
def isolated_dlt_context(tmp_path, monkeypatch):
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    # Cognee imports the nearest .env with override=True. Keep developer/live
    # credentials and model selection out of the offline test process.
    empty_env = tmp_path / "offline.env"
    empty_env.touch()
    monkeypatch.setenv("COGNEE_ENV_FILE", str(empty_env))
    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()


@pytest.fixture
def report():
    return {
        "id": 101,
        "name": "Activation funnel",
        "type": "funnels",
        "description": "Signup then purchase",
        "can_view": True,
        "params": {
            "events": [{"event": "Signup"}, {"event": "Purchase"}],
            "from_date": "-30d",
            "to_date": "today",
        },
    }


@pytest.fixture
def cohort():
    return {
        "id": 202,
        "name": "Activated users",
        "description": "Purchase in seven days",
        "selector": {"operator": "and", "children": [{"behavior": "bhvr_0"}]},
        "behaviors": [{"key": "bhvr_0", "event": "Purchase", "window": 7}],
        "count": 30,
    }


@pytest.fixture
def schema():
    return {
        "entityType": "event",
        "name": "Signup",
        "schemaJson": {
            "description": "An account was created",
            "properties": {
                "plan": {"type": "string", "description": "Billing plan", "examples": ["free"]}
            },
            "metadata": {
                "com.mixpanel": {
                    "tags": ["activation", "signup"],
                    "contacts": ["owner@example.com"],
                }
            },
        },
    }


class FakeClient:
    def __init__(self, directory, report=None, cohort=None, schema=None, scope="synthetic-scope"):
        self.scope = scope
        self.state_directory = directory
        self.project_url = "https://mixpanel.com/project/123"
        self.data = {
            "report": [report] if report else [],
            "cohort": [cohort] if cohort else [],
            "schema": [schema] if schema else [],
        }
        self.access = "read-all-synthetic"
        self.fail_kind = None
        self.inventory_hook = None
        self.reads = 0

    def __enter__(self):
        self.reads = 0
        return self

    def __exit__(self, *args):
        pass

    def access_fingerprint(self):
        return self.access

    def inventory(self, kind):
        self.reads += 1
        if kind == self.fail_kind:
            raise RuntimeError("synthetic partial read")
        if self.inventory_hook:
            return self.inventory_hook(kind, self.reads)
        return copy.deepcopy(self.data[kind])

    def detail(self, kind, remote_id):
        return copy.deepcopy(next(row for row in self.data[kind] if row["id"] == remote_id))
