"""Disable DLT telemetry for offline tests."""

import os
from datetime import datetime, timezone

import pytest

os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"


@pytest.fixture(autouse=True)
def isolated_dlt_context(monkeypatch):
    import cognee_community_connector_twitch.twitch as twitch
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 31, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(twitch, "datetime", Clock)
    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()
