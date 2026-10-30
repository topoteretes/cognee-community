"""The live-verification script itself is exercised against the fake API (no network)."""

import importlib.util
from pathlib import Path

import pytest

from cognee_community_connector_outreach import outreach as mod

SCRIPT = Path(__file__).parent.parent / "examples" / "verify_live.py"


def load_script():
    spec = importlib.util.spec_from_file_location("verify_live", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wired(fake, session_for, token_path, seed_tokens, monkeypatch):
    fake.honor_fields = True
    monkeypatch.setenv("OUTREACH_CLIENT_ID", "cid")
    monkeypatch.setenv("OUTREACH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("OUTREACH_TOKEN_STORE", str(token_path))
    monkeypatch.setattr(mod, "_build_session", lambda **kw: session_for(fake))
    monkeypatch.setattr(mod, "_build_token_session", lambda: session_for(fake))
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_live",
            "--base-url",
            "https://api.outreach.example.test/api/v2",
            "--force-refresh",
        ],
    )
    return fake


def test_verify_live_runs_clean_and_prints_no_secrets_or_content(wired, capsys):
    code = load_script().main()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[PASS] token refresh" in out and "filter[updatedAt] lower bound is inclusive" in out
    assert "pagination via links.next" in out
    for forbidden in ("at-0", "at-1", "rt-0", "rt-1", "csecret", "Pat1", "example.test", "SUBJECT"):
        assert forbidden not in out
    assert all(c[0] != "POST" for c in [("GET",)])  # script only reads (token POST is OAuth)


def test_verify_live_flags_a_missing_scope(wired, capsys):
    wired.status_for[("/calls", "any")] = 403
    code = load_script().main()
    out = capsys.readouterr().out
    assert code == 1 and "[FAIL] calls: endpoint + scope" in out and "calls.read" in out
