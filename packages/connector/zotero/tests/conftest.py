"""Disable telemetry and forbid network access throughout the offline suite."""

import os
import socket

os.environ["DLT_TELEMETRY"] = "false"
os.environ["ENABLE_TELEMETRY"] = "false"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

import pytest


def forbidden(*args, **kwargs):
    raise AssertionError("External sockets are forbidden in Zotero tests")


# Block even import-time networking, before cognee and dlt are imported.
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.getaddrinfo = forbidden


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def isolated_pipeline():
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()
