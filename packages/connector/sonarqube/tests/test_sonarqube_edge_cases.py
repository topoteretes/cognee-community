"""Edge cases and secrecy checks from a pre-commit review.

Q6: no volatile field reaches the rendered content / hash.
Q7: the token never appears in any exception message.
Q10: empty project, missing "rules" key, no textRange/line, missing paging.
"""

import pytest
from fake_sonarqube import FakeResponse, FakeSonarQubeSession, issue

from cognee_community_connector_sonarqube.sonarqube import (
    SonarQubeAPIError,
    _Client,
    _Config,
    _hash_row,
    _iter_rows,
    _list_issues,
    _render_issue,
)


def _config(**overrides):
    defaults = {
        "base_url": "https://sonarqube.example.com",
        "organization": None,
        "project_keys": ("proj",),
        "all_projects": False,
        "branch": None,
        "severities": ("MAJOR", "CRITICAL", "BLOCKER"),
        "issue_types": ("CODE_SMELL", "BUG", "VULNERABILITY"),
        "include_issues": True,
        "include_hotspots": True,
        "include_quality_gate": True,
        "created_after": None,
        "max_issues_per_project": 5000,
        "max_hotspots_per_project": 1000,
        "include_author": False,
        "timeout": 30.0,
        "allow_mass_delete": False,
    }
    defaults.update(overrides)
    return _Config(**defaults)


def _client(session):
    return _Client(session, "https://sonarqube.example.com", 30.0)


def _run(session, config, state):
    return list(_iter_rows(_client(session), config, state, {}))


# ---------------------------------------------------------------------------
# Q6: volatile fields never reach rendered content / the hash
# ---------------------------------------------------------------------------
def test_update_date_and_counters_never_change_the_hash():
    base = issue("A")
    volatile = dict(base, updateDate="2099-12-31T23:59:59+0000", commentsCount=999, hash="xyz")
    row_a = _render_issue(base, "Proj", "proj", _config(), {}, "host")
    row_b = _render_issue(volatile, "Proj", "proj", _config(), {}, "host")
    assert row_a == row_b
    assert _hash_row(row_a) == _hash_row(row_b)


def test_rendering_identical_regardless_of_dict_key_order():
    base = issue("A", tags=["b", "a"])
    reordered = {k: base[k] for k in reversed(list(base.keys()))}
    row_a = _render_issue(base, "Proj", "proj", _config(), {}, "host")
    row_b = _render_issue(reordered, "Proj", "proj", _config(), {}, "host")
    assert row_a == row_b


# ---------------------------------------------------------------------------
# Q7: the token never appears in any exception
# ---------------------------------------------------------------------------
SECRET = "sq_super_secret_abc123"


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_our_own_error_text_never_contains_the_token(status):
    """With no ``errors[].msg`` in the body, every word of the raised message
    comes from our own code (status/path/project/static hints), never from
    SonarQube -- so none of it may ever contain the session's token.
    """
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj"}})
    session.script("/api/components/show", status, {})  # no "errors" key at all
    client = _client(session)
    client.session.headers = {"Authorization": f"Bearer {SECRET}"}
    from cognee_community_connector_sonarqube.sonarqube import _get_json

    with pytest.raises(SonarQubeAPIError) as exc_info:
        _get_json(client, "/api/components/show", {"component": "proj"}, project="proj")
    assert SECRET not in str(exc_info.value)


def test_our_own_code_never_interpolates_the_token_anywhere():
    """The token is only ever used as a header/basic-auth credential, never
    built into a URL, a log message, or an exception by this connector's own
    code (as opposed to a server echoing it back, covered separately).
    """
    from cognee_community_connector_sonarqube import sonarqube_source

    with pytest.raises(ValueError) as exc_info:
        sonarqube_source(
            token=f"{SECRET}\n",  # trailing control char -> _check_token rejects it
            base_url="https://sonarqube.example.com",
            project_keys=["proj"],
        )
    assert SECRET not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Q10: edge cases
# ---------------------------------------------------------------------------
def test_empty_project_zero_issues_zero_hotspots_gate_none():
    session = FakeSonarQubeSession(
        projects={"proj": {"name": "Proj", "issues": [], "hotspots": [], "gate": None}}
    )
    docs = _run(session, _config(), {})
    assert docs == []


def test_missing_rules_key_entirely_does_not_crash():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})

    def no_rules_key(params):
        return FakeResponse(200, {"issues": [issue("A")], "paging": {"total": 1}})

    session._issues_search = no_rules_key
    client = _client(session)
    result, rule_names = _list_issues(client, _config(), "proj")
    assert len(result) == 1
    assert rule_names == {}


def test_issue_without_text_range_or_line_renders_path_without_a_line_number():
    bare = {
        "key": "A",
        "message": "msg",
        "severity": "MAJOR",
        "type": "BUG",
        "component": "proj:x.py",
    }
    row = _render_issue(bare, "Proj", "proj", _config(), {}, "host")
    assert "File: x.py" in row["content"]
    assert "File: x.py:" not in row["content"]  # no stray line-number suffix


def test_issue_without_component_or_line_omits_file_entirely():
    bare = {"key": "A", "message": "msg", "severity": "MAJOR", "type": "BUG"}
    row = _render_issue(bare, "Proj", "proj", _config(), {}, "host")
    assert "File:" not in row["content"]


def test_missing_paging_key_entirely_does_not_crash():
    session = FakeSonarQubeSession(projects={"proj": {"name": "Proj", "issues": [issue("A")]}})

    def no_paging_key(params):
        return FakeResponse(200, {"issues": [issue("A")], "rules": []})

    session._issues_search = no_paging_key
    client = _client(session)
    result, _rules = _list_issues(client, _config(), "proj")
    assert len(result) == 1
