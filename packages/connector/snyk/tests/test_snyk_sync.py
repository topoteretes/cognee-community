"""Dedup and rendering tests: ``_iter_rows`` against an in-memory Snyk. No network, no LLM."""

import copy
import json
import random
from urllib.parse import urlsplit

import pytest
from fake_snyk import FakeResponse, FakeSnykSession, error_response, make_issue, make_project

from cognee_community_connector_snyk import snyk
from cognee_community_connector_snyk.snyk import _Client, _iter_rows, _SnykConfig

TOKEN = "snyk-secret-token-0123456789"
LODASH = "snyk:us-01:o1:vuln:SNYK-JS-LODASH-567746"
PROJECTS = [
    make_project("p1", "acme/shop"),
    make_project("p2", "acme/admin"),
    make_project("p3", "acme/docs", target_file="docs/package.json"),
]


class RecordingLogger:
    def __init__(self):
        self.lines: list[str] = []

    def _record(self, message, *args):
        self.lines.append(message % args if args else message)

    warning = info = debug = _record


@pytest.fixture(autouse=True)
def log(monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(snyk, "logger", recorder)
    monkeypatch.setattr(snyk, "_sleep", lambda seconds: None)
    return recorder


def config(**overrides) -> _SnykConfig:
    values = {
        "rest_base": "https://api.snyk.io/rest",
        "region": "us-01",
        "org_ids": ("o1",),
        "group_id": None,
        "all_orgs": False,
        "project_ids": (),
        "severities": (),
        "issue_types": ("package_vulnerability",),
        "include_resolved": False,
        "include_ignored": False,
        "include_remedies": True,
        "max_projects_per_document": 50,
        "max_issues_per_org": 20_000,
        "api_version": "2024-10-15",
        "timeout": 60.0,
    }
    values.update(overrides)
    return _SnykConfig(**values)


def session_with(issues, projects=PROJECTS, filters=True) -> FakeSnykSession:
    session = FakeSnykSession()
    session.apply_filters = filters
    session.add_org("o1", "Acme", projects=projects, issues=issues)
    return session


def sync(issues, projects=PROJECTS, stats=None, filters=True, **overrides):
    session = session_with(issues, projects, filters)
    rows = list(_iter_rows(_Client(session, TOKEN), config(**overrides), {}, stats))
    return {row["id"]: row for row in rows}


def content(issues, **kwargs) -> str:
    rows = sync(issues, **kwargs)
    assert len(rows) == 1
    return next(iter(rows.values()))["content"]


GOLDEN = """\
Snyk vulnerability: Prototype Pollution in lodash
Snyk id: SNYK-JS-LODASH-567746
CVE: CVE-2020-8203
Weakness: CWE-1321
Type: package vulnerability
Severity: high (CVSS 7.4, CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:H/A:H)
Exploit maturity: proof-of-concept
Organization: Acme (us-01)
Description: Affected versions of lodash are vulnerable to Prototype Pollution.

Affected packages and projects:
- lodash@4.17.11 in project acme/admin (package.json), status open, fix: upgrade
- lodash@4.17.15 in project acme/shop (package.json), status open, fix: upgrade

Remediation:
- Fixed in lodash 4.17.19 (vulnerable ranges: <4.17.19).
- Upgrade to lodash@4.17.19 or higher.

Lineage: Vulnerability SNYK-JS-LODASH-567746 (CVE-2020-8203) affects lodash@4.17.11, which is used by project acme/admin. Vulnerability SNYK-JS-LODASH-567746 (CVE-2020-8203) affects lodash@4.17.15, which is used by project acme/shop. Upgrading lodash to 4.17.19 fixes SNYK-JS-LODASH-567746."""  # noqa: E501


def lodash_in_two_projects(**kwargs):
    return [
        make_issue(project="p1", version="4.17.15", maturity="proof-of-concept", **kwargs),
        make_issue(project="p2", version="4.17.11", maturity="proof-of-concept", **kwargs),
    ]


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------
def test_the_same_snyk_id_in_two_projects_is_one_document():
    stats: dict = {}
    rows = sync(lodash_in_two_projects(), stats=stats)
    assert list(rows) == [LODASH]
    row = rows[LODASH]
    assert row["title"] == "Snyk vulnerability SNYK-JS-LODASH-567746: Prototype Pollution in lodash"
    assert row["_deleted"] is False
    assert "acme/shop" in row["content"]
    assert "acme/admin" in row["content"]
    assert stats["issues"] == 2
    assert stats["vulnerabilities"] == 1


def test_golden_output_for_a_vulnerability_in_two_projects():
    assert content(lodash_in_two_projects()) == GOLDEN


def test_without_a_snyk_id_the_first_cve_is_the_key():
    issues = [make_issue(snyk_id=None, cves=("CVE-2021-0002", "CVE-2021-0001"))]
    rows = sync(issues)
    assert list(rows) == ["snyk:us-01:o1:vuln:CVE-2021-0001"]
    text = rows["snyk:us-01:o1:vuln:CVE-2021-0001"]["content"]
    assert "Snyk id:" not in text
    assert "CVE: CVE-2021-0001, CVE-2021-0002" in text


def test_without_any_id_each_finding_is_its_own_document():
    stats: dict = {}
    issues = [
        make_issue(project="p1", snyk_id=None, cves=(), cwes=(), key="k-1", title="Odd finding"),
        make_issue(project="p2", snyk_id=None, cves=(), cwes=(), key="k-1", title="Odd finding"),
    ]
    rows = sync(issues, stats=stats)
    assert set(rows) == {"snyk:us-01:o1:issue:p1:k-1", "snyk:us-01:o1:issue:p2:k-1"}
    assert stats["fallback_documents"] == 2
    assert rows["snyk:us-01:o1:issue:p1:k-1"]["title"] == "Snyk vulnerability: Odd finding"


def test_two_vulnerabilities_sharing_a_cve_stay_separate_documents():
    issues = [
        make_issue(snyk_id="SNYK-JS-A-1", cves=("CVE-2020-1111",), title="A"),
        make_issue(snyk_id="SNYK-JS-B-2", cves=("CVE-2020-1111",), title="B"),
    ]
    rows = sync(issues)
    assert set(rows) == {"snyk:us-01:o1:vuln:SNYK-JS-A-1", "snyk:us-01:o1:vuln:SNYK-JS-B-2"}
    assert all("CVE: CVE-2020-1111" in row["content"] for row in rows.values())


def test_every_cve_and_cwe_id_is_in_the_text():
    issues = [
        make_issue(project="p1", cves=("CVE-2020-0002",), cwes=("CWE-400",)),
        make_issue(project="p2", cves=("CVE-2020-0001", "CVE-2020-0002"), cwes=("CWE-1321",)),
    ]
    text = content(issues)
    assert "CVE: CVE-2020-0001, CVE-2020-0002" in text
    assert "Weakness: CWE-1321, CWE-400" in text


def test_ids_are_told_apart_by_their_pattern_not_their_source():
    issue = make_issue(snyk_id=None, cves=(), cwes=())
    issue["attributes"]["problems"] = [
        {"id": "SNYK-JS-X-9", "source": "anything", "type": "vulnerability"},
        {"id": "cve-2024-12345", "source": "whatever", "type": "vulnerability"},
        {"id": "not-an-id", "source": "CVE", "type": "vulnerability"},
        {"id": 5, "source": "CVE"},
        "junk",
    ]
    rows = sync([issue])
    text = rows["snyk:us-01:o1:vuln:SNYK-JS-X-9"]["content"]
    assert "CVE: CVE-2024-12345" in text
    assert "not-an-id" not in text


def test_the_affected_list_is_sorted_and_capped():
    projects = [make_project(f"p{i}", f"acme/svc-{i}") for i in range(5)]
    issues = [make_issue(project=f"p{i}") for i in reversed(range(5))]
    text = content(issues, projects=projects, max_projects_per_document=3)
    lines = [line for line in text.splitlines() if line.startswith("- lodash@")]
    assert [line.split(" in project ")[1].split(",")[0].split(" ")[0] for line in lines] == [
        "acme/svc-0",
        "acme/svc-1",
        "acme/svc-2",
    ]
    assert "- and 2 more projects" in text
    assert "acme/svc-3" not in text
    assert "acme/svc-4" not in text


def test_a_package_in_two_manifests_of_one_project_is_listed_once_each():
    projects = [make_project("p1", "acme/shop"), make_project("p2", "acme/shop", "yarn.lock")]
    issues = [make_issue(project="p1"), make_issue(project="p2"), make_issue(project="p1")]
    text = content(issues, projects=projects)
    assert text.count("- lodash@4.17.15 in project acme/shop (package.json)") == 1
    assert text.count("- lodash@4.17.15 in project acme/shop (yarn.lock)") == 1


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_the_output_does_not_depend_on_the_order_of_the_data():
    issues = [*lodash_in_two_projects(), make_issue(project="p3", version="4.17.4")]
    shuffled = copy.deepcopy(issues)
    random.Random(3).shuffle(shuffled)
    for issue in shuffled:
        issue["attributes"]["problems"].reverse()
        issue["attributes"]["classes"].reverse()
    assert sync(issues) == sync(shuffled)


def test_volatile_fields_never_change_the_output():
    first = lodash_in_two_projects()
    second = copy.deepcopy(first)
    for issue in second:
        issue["attributes"]["updated_at"] = "2031-01-01T00:00:00Z"
        issue["attributes"]["created_at"] = "2031-01-01T00:00:00Z"
        issue["attributes"]["risk"] = {"score": {"model": "v5", "value": 999}}
        issue["attributes"]["severities"][0]["modification_time"] = "2031-01-01T00:00:00Z"
        issue["attributes"]["coordinates"][0]["updated_at"] = "2031-01-01T00:00:00Z"
        issue["attributes"]["coordinates"][0]["last_introduced_at"] = "2031-01-01T00:00:00Z"
        issue["relationships"]["test_executions"] = {"data": [{"id": "x"}]}
    assert sync(first) == sync(second)
    text = content(first)
    for volatile in ("2026-10-01", "risk", "500", "Z"):
        assert volatile not in text.replace("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:H/A:H", "")


def test_remedies_can_be_turned_off():
    text = content(lodash_in_two_projects(), include_remedies=False)
    assert "Remediation" not in text
    assert "Fixed in" not in text
    assert "Upgrading" not in text
    assert "fix: upgrade" in text  # the fix flag is not remedy text


def test_fix_versions_are_sorted_naturally_and_merged_across_projects():
    issues = [
        make_issue(project="p1", fixed_in=("4.17.10", "4.17.9"), ranges=("<4.17.9",)),
        make_issue(project="p2", fixed_in=("4.17.2",), ranges=(">=5 <5.1",)),
    ]
    text = content(issues)
    assert (
        "- Fixed in lodash 4.17.2, 4.17.9, 4.17.10 (vulnerable ranges: <4.17.9; >=5 <5.1)." in text
    )


def test_remedy_text_and_description_are_clipped():
    issue = make_issue(remedy="r" * 900, description="d " * 900)
    text = content([issue])
    description = next(line for line in text.splitlines() if line.startswith("Description:"))
    assert description.endswith("...")
    assert len(description) <= len("Description: ") + 800
    remedy = next(line for line in text.splitlines() if line.startswith("- rrr"))
    assert remedy.endswith("...")
    assert len(remedy) <= 2 + 500


def test_at_most_five_distinct_remedies_are_listed():
    issues = [make_issue(project="p1", remedy=f"remedy {i}") for i in range(8)]
    text = content(issues)
    assert len([line for line in text.splitlines() if line.startswith("- remedy ")]) == 5


def test_fix_labels_follow_the_coordinate_flags():
    issue = make_issue(upgradeable=False)
    issue["attributes"]["coordinates"][0]["is_pinnable"] = True
    assert "fix: pin" in content([issue])
    assert "fix: none" in content([make_issue(upgradeable=False)])


def test_the_highest_severity_and_cvss_win():
    issues = [
        make_issue(project="p1", severity="medium", score=5.0, vector="V:LOW"),
        make_issue(project="p2", severity="critical", score=9.8, vector="V:HIGH"),
    ]
    assert "Severity: critical (CVSS 9.8, V:HIGH)" in content(issues)
    assert "Severity: low" in content([make_issue(severity="low", score=None)])


def test_a_url_comes_only_from_the_problems():
    assert "url" not in next(iter(sync([make_issue()]).values()))
    row = next(
        iter(sync([make_issue(url="https://security.snyk.io/vuln/SNYK-JS-LODASH-567746")]).values())
    )
    assert row["url"] == "https://security.snyk.io/vuln/SNYK-JS-LODASH-567746"


def test_an_unknown_project_renders_as_its_id():
    text = content([make_issue(project="ghost")], projects=[])
    assert "in project ghost, status open" in text


def test_the_organization_falls_back_to_its_id():
    session = FakeSnykSession()
    session.add_org("o1", "", projects=PROJECTS, issues=[make_issue()])
    session.orgs["o1"] = ""
    rows = list(_iter_rows(_Client(session, TOKEN), config(), {}))
    assert "Organization: o1 (us-01)" in rows[0]["content"]


def test_unknown_shapes_are_tolerated_and_counted():
    weird = make_issue(project="p1")
    weird["attributes"]["coordinates"] = [
        "junk",
        {"representations": []},
        {"representations": [{"resourcePath": "x"}]},
        {
            "state": "open",
            "representations": [
                {"dependency": {"package_name": "left-pad", "package_version": "1.0.0"}}
            ],
            "remedies": ["junk", {"meta": None}],
        },
    ]
    bare = {
        "id": "x",
        "type": "issue",
        "attributes": {"title": "Bare", "problems": [{"id": "SNYK-JS-BARE-1"}]},
    }
    stats: dict = {}
    # The fake cannot filter shapeless items, so it hands them over as they are.
    rows = sync([weird, bare, {"id": "y"}, "junk"], stats=stats, filters=False)
    assert stats["skipped_coordinates"] == 3
    assert "- left-pad@1.0.0 in project acme/shop" in rows[LODASH]["content"]
    assert (
        "- (package unknown) in project unknown, status open, fix: none"
        in (rows["snyk:us-01:o1:vuln:SNYK-JS-BARE-1"]["content"])
    )


def test_sparse_issues_are_counted_and_warned_about_once_per_run(log):
    issues = [make_issue(snyk_id=None, cves=(), cwes=()) for _ in range(3)]
    for issue in issues:
        issue["attributes"]["problems"] = []
    issues.append(make_issue(project="p2"))
    issues[-1]["attributes"]["coordinates"] = []
    stats: dict = {}
    sync(issues, stats=stats)
    assert stats["sparse_issues"] == 4
    assert sum("without `problems` or `coordinates`" in line for line in log.lines) == 1
    sync(issues)
    assert sum("without `problems` or `coordinates`" in line for line in log.lines) == 2


def test_complete_issues_raise_no_sparse_warning(log):
    stats: dict = {}
    sync(lodash_in_two_projects(), stats=stats)
    assert stats["sparse_issues"] == 0
    assert not any("without `problems`" in line for line in log.lines)


# ---------------------------------------------------------------------------
# Status filters, limits and empty organizations
# ---------------------------------------------------------------------------
def mixed_issues():
    return [
        make_issue(project="p1", snyk_id="SNYK-JS-OPEN-1", title="Open"),
        make_issue(project="p1", snyk_id="SNYK-JS-GONE-2", title="Resolved", status="resolved"),
        make_issue(project="p1", snyk_id="SNYK-JS-QUIET-3", title="Ignored", ignored=True),
    ]


def test_resolved_and_ignored_findings_are_left_out_by_default():
    assert set(sync(mixed_issues())) == {"snyk:us-01:o1:vuln:SNYK-JS-OPEN-1"}


def test_the_connector_filters_again_when_the_api_does_not():
    stats: dict = {}
    rows = sync(mixed_issues(), filters=False, stats=stats)
    assert set(rows) == {"snyk:us-01:o1:vuln:SNYK-JS-OPEN-1"}
    assert stats["skipped_issues"] == 2


def test_resolved_and_ignored_findings_can_be_included():
    rows = sync(mixed_issues(), include_resolved=True, include_ignored=True)
    assert len(rows) == 3
    assert "status resolved" in rows["snyk:us-01:o1:vuln:SNYK-JS-GONE-2"]["content"]
    assert "status open, ignored" in rows["snyk:us-01:o1:vuln:SNYK-JS-QUIET-3"]["content"]


def test_too_many_issues_raise_instead_of_being_cut_short():
    issues = [make_issue(snyk_id=f"SNYK-JS-N-{i}") for i in range(5)]
    with pytest.raises(RuntimeError, match="organization o1 has more than 4"):
        sync(issues, max_issues_per_org=4)
    assert len(sync(issues, max_issues_per_org=5)) == 5


def test_an_empty_organization_yields_nothing_and_warns(log):
    stats: dict = {}
    assert sync([], stats=stats) == {}
    assert any("o1 returned no matching issues" in line for line in log.lines)
    assert stats["orgs"] == 1


def test_an_organization_without_projects_is_fine(log):
    assert sync([], projects=[]) == {}


def test_only_package_vulnerabilities_are_read_by_default():
    issues = [make_issue(), make_issue(issue_type="license", snyk_id="SNYK-LIC-1", title="GPL")]
    assert set(sync(issues)) == {LODASH}
    assert len(sync(issues, issue_types=("package_vulnerability", "license"))) == 2


def test_a_failure_in_a_later_org_emits_nothing():
    session = session_with([make_issue()])
    session.add_org("o2", "Second", issues=[make_issue(org="o2")])
    session.script = []

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    original = session.get
    count = {"n": 0}

    def get(url, **kwargs):
        if "/orgs/o2/issues" in url:
            broken()
        count["n"] += 1
        return original(url, **kwargs)

    session.get = get
    iterator = _iter_rows(_Client(session, TOKEN), config(org_ids=("o1", "o2")), {})
    with pytest.raises(RuntimeError, match="boom"):
        next(iterator)


def test_stats_count_orgs_projects_issues_and_documents():
    stats: dict = {}
    sync(lodash_in_two_projects(), stats=stats)
    assert stats == {
        "orgs": 1,
        "projects": 3,
        "issues": 2,
        "vulnerabilities": 1,
        "emitted": 1,
        "unchanged": 0,
        "deleted": 0,
        "kept": 0,
        "spot_checks": 0,
        "skipped_issues": 0,
        "skipped_coordinates": 0,
        "fallback_documents": 0,
        "sparse_issues": 0,
    }


# ---------------------------------------------------------------------------
# Incremental sync, tombstones and guards
# ---------------------------------------------------------------------------
def vuln(n: int, org: str = "o1") -> str:
    return f"snyk:us-01:{org}:vuln:SNYK-JS-V{n}"


def finding(n: int, project: str = "p1", **kwargs):
    """A finding of vulnerability ``n`` (its own Snyk id and CVE) with a known issue id."""
    return make_issue(
        project=project,
        snyk_id=f"SNYK-JS-V{n}",
        cves=(f"CVE-2020-{1000 + n}",),
        title=f"Vuln {n}",
        package=f"pkg{n}",
        issue_id=f"i-{n}-{project}",
        **kwargs,
    )


def world_of(issues, org: str = "o1") -> FakeSnykSession:
    session = FakeSnykSession()
    session.add_org(org, "Acme", projects=PROJECTS, issues=issues)
    return session


def step(session, state, **overrides):
    """One sync run: (tombstone ids, upserted rows by id, stats, all rows in order)."""
    stats: dict = {}
    rows = list(_iter_rows(_Client(session, TOKEN), config(**overrides), state, stats))
    deleted = [r["id"] for r in rows if r["_deleted"]]
    return deleted, {r["id"]: r for r in rows if not r["_deleted"]}, stats, rows


def snapshot(state) -> str:
    return json.dumps(state, sort_keys=True)


def probes(session) -> list[dict]:
    """Requests for one issue by id (the spot-checks)."""
    return [c for c in session.calls if "/issues/" in urlsplit(c["url"]).path]


def issue_of(session, issue_id: str, org: str = "o1") -> dict:
    return next(i for i in session.issues[org] if i["id"] == issue_id)


def bulk(count: int = 10) -> list[dict]:
    return [finding(n) for n in range(count)]


def test_first_run_emits_everything_and_fills_the_state():
    session = world_of([finding(1), finding(2, "p2"), finding(1, "p2")])
    state: dict = {}
    deleted, upserts, stats, _ = step(session, state)
    assert deleted == []
    assert set(upserts) == {vuln(1), vuln(2)}
    docs = state["orgs"]["us-01:o1"]["docs"]
    assert set(docs) == {vuln(1), vuln(2)}
    assert all(len(d["hash"]) == 64 for d in docs.values())
    assert docs[vuln(1)]["issues"] == ["i-1-p1", "i-1-p2"]
    assert stats["emitted"] == 2
    assert stats["unchanged"] == 0


def test_the_state_keeps_at_most_three_sorted_issue_ids():
    session = world_of([finding(1, p) for p in ("p3", "p1", "p2")] + [finding(1, "p4")])
    state: dict = {}
    step(session, state, project_ids=())
    assert state["orgs"]["us-01:o1"]["docs"][vuln(1)]["issues"] == ["i-1-p1", "i-1-p2", "i-1-p3"]


def test_an_unchanged_second_run_emits_nothing_with_the_expected_calls():
    session = world_of([finding(1), finding(2, "p2")])
    state: dict = {}
    step(session, state)
    before, calls = snapshot(state), len(session.calls)
    _, _, stats, rows = step(session, state)
    assert rows == []
    assert [urlsplit(c["url"]).path for c in session.calls[calls:]] == [
        "/rest/orgs/o1",
        "/rest/orgs/o1/projects",
        "/rest/orgs/o1/issues",
    ]
    assert stats["unchanged"] == 2
    assert snapshot(state) == before


def test_a_new_finding_of_a_known_vulnerability_re_emits_only_that_document():
    session = world_of([finding(1), finding(2)])
    state: dict = {}
    step(session, state)
    session.issues["o1"].append(finding(1, "p3"))
    deleted, upserts, stats, _ = step(session, state)
    assert deleted == []
    assert list(upserts) == [vuln(1)]
    assert "acme/shop" in upserts[vuln(1)]["content"]
    assert "acme/docs" in upserts[vuln(1)]["content"]
    assert stats["unchanged"] == 1


def test_resolving_one_of_two_projects_re_emits_without_it_and_the_last_forgets_it():
    session = world_of([finding(1), finding(1, "p2"), finding(2)])
    state: dict = {}
    step(session, state)
    issue_of(session, "i-1-p2")["attributes"]["status"] = "resolved"
    deleted, upserts, _, _ = step(session, state)
    assert deleted == []
    assert "acme/admin" not in upserts[vuln(1)]["content"]
    assert "acme/shop" in upserts[vuln(1)]["content"]
    issue_of(session, "i-1-p1")["attributes"]["status"] = "resolved"
    deleted, upserts, _, _ = step(session, state)
    assert deleted == [vuln(1)]
    assert upserts == {}
    assert set(state["orgs"]["us-01:o1"]["docs"]) == {vuln(2)}


def test_ignoring_behaves_like_resolving_by_default():
    session = world_of([finding(1), finding(1, "p2")])
    state: dict = {}
    step(session, state)
    issue_of(session, "i-1-p2")["attributes"]["ignored"] = True
    _, upserts, _, _ = step(session, state)
    assert "acme/admin" not in upserts[vuln(1)]["content"]
    issue_of(session, "i-1-p1")["attributes"]["ignored"] = True
    deleted, _, _, _ = step(session, state)
    assert deleted == [vuln(1)]


def test_with_include_resolved_and_include_ignored_the_document_stays_with_its_status():
    session = world_of([finding(1), finding(1, "p2")])
    state: dict = {}
    step(session, state, include_resolved=True, include_ignored=True)
    issue_of(session, "i-1-p1")["attributes"]["status"] = "resolved"
    issue_of(session, "i-1-p2")["attributes"]["ignored"] = True
    deleted, upserts, _, _ = step(session, state, include_resolved=True, include_ignored=True)
    assert deleted == []
    text = upserts[vuln(1)]["content"]
    assert "acme/shop (package.json), status resolved" in text
    assert "acme/admin (package.json), status open, ignored" in text


def test_a_remediation_change_re_emits_but_volatile_changes_do_not():
    session = world_of([finding(1), finding(2)])
    state: dict = {}
    step(session, state)
    for issue in session.issues["o1"]:
        issue["attributes"]["updated_at"] = "2031-01-01T00:00:00Z"
        issue["attributes"]["risk"] = {"score": {"model": "v9", "value": 1}}
        issue["attributes"]["coordinates"][0]["updated_at"] = "2031-01-01T00:00:00Z"
    assert step(session, state)[3] == []
    data = issue_of(session, "i-1-p1")["attributes"]["coordinates"][0]["remedies"][0]["meta"][
        "data"
    ]
    data["fixed_in"] = ["9.9.9"]
    _, upserts, _, _ = step(session, state)
    assert list(upserts) == [vuln(1)]
    assert "9.9.9" in upserts[vuln(1)]["content"]


def test_a_deleted_project_shrinks_or_forgets_what_it_had():
    session = world_of([finding(1), finding(1, "p2"), finding(2, "p2")])
    state: dict = {}
    step(session, state)
    del session.projects["o1"]["p2"]
    session.issues["o1"] = [i for i in session.issues["o1"] if not i["id"].endswith("-p2")]
    deleted, upserts, _, _ = step(session, state)
    assert deleted == [vuln(2)]
    assert list(upserts) == [vuln(1)]
    assert "acme/admin" not in upserts[vuln(1)]["content"]


def test_other_regions_in_the_state_are_left_alone():
    session = world_of([finding(1)])
    state = {"orgs": {"eu-01:o9": {"docs": {"x": {"hash": "h", "issues": ["i"]}}}}}
    deleted, _, _, _ = step(session, state)
    assert deleted == []
    assert state["orgs"]["eu-01:o9"]["docs"] == {"x": {"hash": "h", "issues": ["i"]}}


# --- guard and spot-check ---------------------------------------------------
def test_a_bulk_disappearance_that_spot_checks_confirm_goes_through():
    session = world_of(bulk())
    state: dict = {}
    step(session, state)
    session.issues["o1"] = session.issues["o1"][6:]  # six findings are gone: 404 by id
    deleted, _, stats, _ = step(session, state)
    assert deleted == [vuln(n) for n in range(6)]
    assert stats["spot_checks"] == 5
    assert len(probes(session)) == 5


def test_findings_that_are_merely_filtered_out_also_confirm_the_deletion():
    session = world_of(bulk())
    state: dict = {}
    step(session, state)
    for issue in session.issues["o1"][:6]:
        issue["attributes"]["status"] = "resolved"  # 200 by id, but not in the selection
    deleted, _, stats, _ = step(session, state)
    assert len(deleted) == 6
    assert stats["spot_checks"] == 5


def test_a_spot_check_that_finds_a_live_finding_refuses_and_keeps_the_state():
    session = world_of(bulk())
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    live = finding(0)  # still open, but the listing no longer shows it
    session.issues["o1"] = session.issues["o1"][6:]
    session.fixed["/orgs/o1/issues/i-0-p1"] = FakeResponse(200, {"data": live})
    with pytest.raises(RuntimeError, match="still exists") as error:
        step(session, state)
    assert "allow_mass_delete=True" in str(error.value)
    assert "silently" in str(error.value)
    assert snapshot(state) == before


def test_a_spot_check_that_cannot_be_answered_refuses():
    session = world_of(bulk())
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    session.issues["o1"] = session.issues["o1"][6:]
    session.fixed["/orgs/o1/issues/i-0-p1"] = error_response(403, "Forbidden")
    with pytest.raises(RuntimeError, match="HTTP 403"):
        step(session, state)
    assert snapshot(state) == before


def test_allow_mass_delete_skips_the_guard_and_the_spot_checks():
    session = world_of(bulk())
    state: dict = {}
    step(session, state)
    session.issues["o1"] = session.issues["o1"][8:]
    session.fixed["/orgs/o1/issues/i-0-p1"] = FakeResponse(200, {"data": finding(0)})
    deleted, _, stats, _ = step(session, state, allow_mass_delete=True)
    assert len(deleted) == 8
    assert probes(session) == []
    assert stats["spot_checks"] == 0


@pytest.mark.parametrize("gone", [1, 4, 5])
def test_a_small_deletion_is_trusted_without_a_spot_check(gone):
    session = world_of(bulk())  # 5 of 10 is not more than half
    state: dict = {}
    step(session, state)
    session.issues["o1"] = session.issues["o1"][gone:]
    deleted, _, stats, _ = step(session, state)
    assert len(deleted) == gone
    assert probes(session) == []
    assert stats["spot_checks"] == 0


def test_an_organization_where_everything_was_fixed_is_forgotten():
    session = world_of(bulk(6))
    state: dict = {}
    step(session, state)
    for issue in session.issues["o1"]:
        issue["attributes"]["status"] = "resolved"
    deleted, _, stats, _ = step(session, state)
    assert len(deleted) == 6
    assert stats["spot_checks"] == 5
    assert state["orgs"]["us-01:o1"]["docs"] == {}


def test_a_small_organization_that_empties_is_checked_too():
    session = world_of([finding(1), finding(2)])
    state: dict = {}
    step(session, state)
    for issue in session.issues["o1"]:
        issue["attributes"]["status"] = "resolved"
    assert len(step(session, state)[0]) == 2
    assert len(probes(session)) == 2


def test_silent_permission_loss_is_refused():
    session = world_of(bulk(3))
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    session.hide_listing = True  # the listing is empty, the findings are still there
    with pytest.raises(RuntimeError, match="returned no matching findings"):
        step(session, state)
    assert snapshot(state) == before
    assert len(step(session, state, allow_mass_delete=True)[0]) == 3


def test_an_empty_listing_with_an_empty_state_is_not_an_error():
    state: dict = {}
    assert step(world_of([]), state)[3] == []
    assert state["orgs"]["us-01:o1"]["docs"] == {}


# --- organizations ----------------------------------------------------------
def two_org_world() -> FakeSnykSession:
    session = FakeSnykSession()
    session.add_org("o1", "One", projects=PROJECTS, issues=[finding(1)])
    session.add_org("o2", "Two", projects=PROJECTS, issues=[finding(2, org="o2")])
    return session


def test_an_org_removed_from_an_explicit_list_is_forgotten_without_a_request():
    session = two_org_world()
    state: dict = {}
    step(session, state, org_ids=("o1", "o2"))
    calls = len(session.calls)
    deleted, _, _, _ = step(session, state, org_ids=("o1",))
    assert deleted == [vuln(2, "o2")]
    assert set(state["orgs"]) == {"us-01:o1"}
    assert not any("/orgs/o2" in c["url"] for c in session.calls[calls:])


def dynamic_world():
    session = two_org_world()
    session.group_orgs["g1"] = ["o1", "o2"]
    state: dict = {}
    step(session, state, org_ids=(), group_id="g1")
    session.group_orgs["g1"] = ["o1"]
    return session, state


def test_a_dynamic_org_that_is_gone_is_forgotten():
    session, state = dynamic_world()
    del session.orgs["o2"]  # GET /orgs/o2 now answers 404
    deleted, _, _, _ = step(session, state, org_ids=(), group_id="g1")
    assert deleted == [vuln(2, "o2")]
    assert set(state["orgs"]) == {"us-01:o1"}


def test_a_dynamic_org_that_still_exists_means_an_inconsistent_listing():
    session, state = dynamic_world()
    before = snapshot(state)
    with pytest.raises(RuntimeError, match="still exists"):
        step(session, state, org_ids=(), group_id="g1")
    assert snapshot(state) == before


def test_a_dynamic_org_that_cannot_be_checked_is_kept_with_a_warning(log):
    session, state = dynamic_world()
    session.fixed["/orgs/o2"] = error_response(403, "Forbidden")
    deleted, _, stats, _ = step(session, state, org_ids=(), group_id="g1")
    assert deleted == []
    assert stats["kept"] == 1
    assert "us-01:o2" in state["orgs"]
    assert any("could not be checked (HTTP 403)" in line for line in log.lines)


def test_at_most_twenty_absent_orgs_are_checked_per_run(log):
    session = FakeSnykSession()
    ids = [f"o{n:02d}" for n in range(23)]
    for org in ids:
        session.add_org(org, org, projects=PROJECTS, issues=[finding(1, org=org)])
    session.group_orgs["g1"] = ids
    state: dict = {}
    step(session, state, org_ids=(), group_id="g1")
    session.group_orgs["g1"] = ids[:1]
    for org in ids[1:]:
        del session.orgs[org]
    calls = len(session.calls)
    deleted, _, stats, _ = step(session, state, org_ids=(), group_id="g1")
    checked = [c for c in session.calls[calls:] if urlsplit(c["url"]).path.count("/") == 3]
    assert len(deleted) == 20
    assert stats["kept"] == 2
    assert len(state["orgs"]) == 3
    assert len(checked) == 20  # one GET /orgs/{id} per absent org, and no more
    assert any("too many organizations" in line for line in log.lines)


# --- errors and secrets -------------------------------------------------------
@pytest.mark.parametrize("path", ["/orgs/o1", "/orgs/o1/projects", "/orgs/o1/issues"])
def test_a_failed_request_leaves_the_state_untouched(path):
    session = world_of(bulk(3))
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    session.fixed[path] = error_response(403, "Forbidden")
    session.hide_listing = True  # must not be read as "everything was fixed"
    with pytest.raises(RuntimeError, match="HTTP 403"):
        step(session, state)
    assert snapshot(state) == before


def test_a_failure_in_a_later_organization_leaves_the_state_untouched():
    session = two_org_world()
    state: dict = {}
    step(session, state, org_ids=("o1", "o2"))
    before = snapshot(state)
    issue_of(session, "i-1-p1")["attributes"]["status"] = "resolved"
    session.fixed["/orgs/o2/issues"] = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        step(session, state, org_ids=("o1", "o2"))
    assert snapshot(state) == before


def test_an_interrupted_run_does_not_save_the_state():
    session = world_of([finding(1), finding(2)])
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    issue_of(session, "i-1-p1")["attributes"]["status"] = "resolved"
    iterator = _iter_rows(_Client(session, TOKEN), config(), state)
    next(iterator)
    iterator.close()
    assert snapshot(state) == before


def test_state_and_logs_hold_no_token_and_no_url(log):
    session = world_of(bulk(3))
    state: dict = {}
    step(session, state)
    dumped = json.dumps(state)
    assert json.loads(dumped) == state
    assert set(state) == {"orgs"}
    assert TOKEN not in dumped
    assert "http" not in dumped
    assert TOKEN not in "\n".join(log.lines)
