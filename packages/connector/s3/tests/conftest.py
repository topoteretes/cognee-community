"""Test safety net: boto3 must never see real AWS credentials.

Every test gets fake credentials and a non-existent shared credentials/config
file, so boto3's credential chain cannot fall back to ``~/.aws``; and every test
runs inside moto's ``mock_aws``, so no request can reach real AWS.
"""

import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _fake_aws(tmp_path, monkeypatch):
    missing = str(tmp_path / "no-such-aws-file")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", missing)
    monkeypatch.setenv("AWS_CONFIG_FILE", missing)
    # Don't let a stray profile or region from the developer's shell leak in.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    with mock_aws():
        yield
