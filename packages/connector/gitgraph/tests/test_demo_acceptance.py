"""Acceptance: the ``cognee-gitgraph demo`` script, run keyless with every LLM call counted.

Given the built-in sample repo, when the demo analyses the PR branch, then the blast radius, the CI
failures and the Cognee graph come out right and no LLM was called.
"""

import json
import subprocess
import sys
from typing import Any

import pytest
from test_cli import SEND_RECEIPT_PATH, _keyless

# Every way cognee reaches an LLM: the gateway's three entry points, and litellm underneath them.
# Each is replaced by a function that counts the call and fails, so a call cannot go unnoticed.
_COUNT_LLM_CALLS = """
import cognee
import litellm
from cognee.infrastructure.llm.LLMGateway import LLMGateway

calls = []

def _spy(name):
    def spy(*args, **kwargs):
        calls.append(name)
        raise RuntimeError("LLM call: " + name)
    return spy

for owner, name in [
    (LLMGateway, "acreate_structured_output"),
    (LLMGateway, "create_transcript"),
    (LLMGateway, "transcribe_image"),
    (litellm, "acompletion"),
    (litellm, "completion"),
]:
    setattr(owner, name, staticmethod(_spy(name)) if owner is LLMGateway else _spy(name))
"""
# Runs the real CLI in-process (stdout captured) and reports the calls counted.
_RUN_DEMO = (
    _COUNT_LLM_CALLS
    + """
import contextlib, io, json, sys
from cognee_community_connector_gitgraph.cli import main

out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = main(sys.argv[1:])
print(json.dumps({"code": code, "stdout": out.getvalue(), "llm_calls": calls}))
"""
)
# Not the demo: one deliberate gateway call, to show the counter notices.
_PROBE = (
    _COUNT_LLM_CALLS
    + """
import asyncio, json
from cognee.infrastructure.llm.LLMGateway import LLMGateway

async def ask():
    try:
        await LLMGateway.acreate_structured_output("hi", "be brief", str)
    except RuntimeError:
        pass

asyncio.run(ask())
print(json.dumps({"llm_calls": calls}))
"""
)
# Env var names whose presence would let cognee reach a hosted model.
_KEYS = ("LLM_API_KEY", "OPENAI_API_KEY", "EMBEDDING_API_KEY")


def _python(script: str, *argv: str) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-c", script, *argv],
        capture_output=True,
        text=True,
        check=False,
        env=_keyless(),
    )
    assert done.returncode == 0, done.stderr
    report: dict[str, Any] = json.loads(done.stdout.splitlines()[-1])
    return report


@pytest.fixture(scope="module")
def text_demo() -> dict[str, Any]:
    return _python(_RUN_DEMO, "demo")


@pytest.fixture(scope="module")
def json_demo() -> dict[str, Any]:
    return _python(_RUN_DEMO, "demo", "--json")


@pytest.mark.integration
def test_the_call_counter_notices_a_gateway_call() -> None:
    """Given the spy on cognee's LLM gateway, when one call is made, then it is counted."""
    assert _python(_PROBE)["llm_calls"] == ["acreate_structured_output"]


@pytest.mark.integration
def test_given_no_api_keys_the_demo_makes_zero_llm_calls(
    text_demo: dict[str, Any], json_demo: dict[str, Any]
) -> None:
    """Given no LLM or embedding key, when the demo runs, then it succeeds with zero LLM calls."""
    env = _keyless()  # given
    assert not [key for key in _KEYS if key in env]

    # when: the two demo runs of the fixtures; then
    for run in (text_demo, json_demo):
        assert run["code"] == 0
        assert run["llm_calls"] == []


@pytest.mark.integration
def test_the_json_names_exactly_the_three_at_risk_services(json_demo: dict[str, Any]) -> None:
    """Given a PR changing only shared/auth.py, when analysed, then 3 services are at risk."""
    result = json.loads(json_demo["stdout"])

    assert result["changed_files"] == ["shared/auth.py"]
    assert {s["name"]: s["hops"] for s in result["services"]} == {
        "billing": 1,
        "orders": 2,
        "notifications": 4,
    }
    assert "search" not in {s["name"] for s in result["services"]}


@pytest.mark.integration
def test_the_json_shows_the_four_hop_path_in_full(json_demo: dict[str, Any]) -> None:
    """Given the notifications service, when it is reported, then its whole 4-hop path is shown."""
    services = {s["name"]: s for s in json.loads(json_demo["stdout"])["services"]}

    assert " -> ".join(services["notifications"]["path"]) == SEND_RECEIPT_PATH


@pytest.mark.integration
def test_the_json_lists_the_failing_ci_jobs_and_tests(json_demo: dict[str, Any]) -> None:
    """Given the CI logs, when analysed, then billing-tests and orders-tests list their failures."""
    failures = json.loads(json_demo["stdout"])["ci_failures"]

    assert sorted(f["job"] for f in failures) == ["billing-tests", "orders-tests"]
    by_job = {f["job"]: f for f in failures}
    assert by_job["billing-tests"]["service"] == "billing"
    assert "test_charge_with_max_age" in by_job["billing-tests"]["test_id"]
    assert by_job["orders-tests"]["service"] == "orders"
    assert "test_checkout_rejects_bad_token" in by_job["orders-tests"]["test_id"]


@pytest.mark.integration
def test_the_json_says_the_cognee_graph_got_the_rows_and_links(json_demo: dict[str, Any]) -> None:
    """Given the demo remembers the repo, when it ends, then the graph holds the rows and links."""
    graph = json.loads(json_demo["stdout"])["cognee"]

    assert graph["rows"] == {
        "ci_failures": 2,
        "commits": 8,
        "file_changes": 15,
        "references": 10,
        "symbols": 8,
    }
    assert graph["links"]["sha_references_commits"] == 15
    assert sum(graph["links"].values()) > 0


@pytest.mark.integration
def test_the_text_report_shows_services_path_ci_and_graph(text_demo: dict[str, Any]) -> None:
    """Given the real CLI, when run without --json, then the report tells the whole story."""
    out = text_demo["stdout"]

    assert "Blast radius of main...feature/rotate-tokens" in out
    assert "  billing  1 hop  " in out
    assert "  orders  2 hops  " in out
    assert "  notifications  4 hops  " + SEND_RECEIPT_PATH in out
    assert "search" not in out
    assert "billing-tests" in out
    assert "orders-tests" in out
    assert "test_charge_with_max_age" in out
    assert "test_checkout_rejects_bad_token" in out
    for row in ("ci_failures: 2", "commits: 8", "file_changes: 15", "references: 10"):
        assert f"  {row}\n" in out
    assert "  symbols: 8" in out
    assert "sha_references_commits: 15" in out


@pytest.mark.integration
def test_the_blast_is_read_back_from_cognees_graph(
    text_demo: dict[str, Any], json_demo: dict[str, Any]
) -> None:
    """Given the demo remembers the repo, when it reports, then it walked Cognee's graph."""
    assert json.loads(json_demo["stdout"])["computed_from"] == "cognee graph"
    assert "Callers found by walking Cognee's graph\n" in text_demo["stdout"]
