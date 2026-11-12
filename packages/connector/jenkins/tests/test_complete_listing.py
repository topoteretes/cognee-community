"""Deletion contract: complete Jenkins inventories are the default authority."""

from copy import deepcopy

import pytest
from test_jenkins import BASE_URL, FakeJenkins, _build, _deleted

from cognee_community_connector_jenkins.jenkins import sync_jenkins


def seeded():
    fake = FakeJenkins({"api": {"builds": [_build(1), _build(2)]}, "web": {"builds": [_build(1)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state))
    return fake, state


def test_complete_listing_deletes_job_and_rotated_build_without_callback():
    fake, state = seeded()
    fake.folders = {"": ["api"]}
    fake.jobs["api"]["builds"] = [_build(2), _build(3)]
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert set(_deleted(rows)) == {"job:web", "build:web#1", "build:api#1"}
    assert set(state["jobs"]) == {"api"}
    assert state["jobs"]["api"]["builds"] == [2, 3]


def test_complete_empty_listing_deletes_last_job():
    fake, state = seeded()
    fake.folders = {"": []}
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert set(_deleted(rows)) == {
        "job:api",
        "build:api#1",
        "build:api#2",
        "job:web",
        "build:web#1",
    }
    assert state == {"jobs": {}}


@pytest.mark.parametrize("decision", [False, None, 1, True])
def test_optional_confirmation_is_an_additional_guard(decision):
    fake, state = seeded()
    before = deepcopy(state)
    fake.folders = {"": []}
    calls = []

    def confirm(name, number):
        calls.append((name, number))
        return decision

    rows = list(sync_jenkins(fake, BASE_URL, state, confirm_deleted=confirm))
    assert calls == [("api", None), ("web", None)]
    if decision is True:
        assert len(_deleted(rows)) == 5
        assert state == {"jobs": {}}
    else:
        assert rows == []
        assert state == before


def test_removing_explicit_selection_keeps_unselected_history():
    fake, state = seeded()
    rows = list(sync_jenkins(fake, BASE_URL, state, job_names=["api"]))
    assert _deleted(rows) == []
    assert "web" in state["jobs"]


def test_rotation_below_cursor_requires_reading_older_pages():
    fake = FakeJenkins({"api": {"builds": [_build(n) for n in range(1, 252)]}})
    state = {}
    list(sync_jenkins(fake, BASE_URL, state, max_builds_per_job=0))
    fake.jobs["api"]["builds"] = [_build(n) for n in range(2, 253)]
    rows = list(sync_jenkins(fake, BASE_URL, state))
    assert _deleted(rows) == ["build:api#1"]
    assert state["jobs"]["api"]["builds"] == list(range(2, 253))


@pytest.mark.parametrize("mode", ["partial", "missing", "permission", "depth"])
def test_incomplete_discovery_never_deletes_even_with_callback(mode):
    fake, state = seeded()
    before = deepcopy(state)

    class Broken(FakeJenkins):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url == BASE_URL + "/api/json":
                if mode == "partial":
                    response._payload = {"jobs": [], "partial": True}
                elif mode == "missing":
                    response._payload = {}
                elif mode == "permission":
                    response.status_code = 403
            return response

    client = Broken(fake.jobs, {"": ["team"], "team": []} if mode == "depth" else {"": []})
    with pytest.raises(RuntimeError):
        list(
            sync_jenkins(
                client, BASE_URL, state, max_folder_depth=0, confirm_deleted=lambda *_: True
            )
        )
    assert state == before


@pytest.mark.parametrize("mode", ["missing", "partial", "missing_page", "permission"])
def test_incomplete_build_inventory_never_deletes(mode):
    fake, state = seeded()
    before = deepcopy(state)

    class Broken(FakeJenkins):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url.endswith("/job/api/api/json"):
                tree = kwargs["params"]["tree"]
                if mode == "missing" or (mode == "missing_page" and "{0,100}" not in tree):
                    response._payload.pop("allBuilds", None)
                elif mode == "partial":
                    response._payload["hasMore"] = True
                elif mode == "permission":
                    response.status_code = 403
            return response

    client = Broken({"api": {"builds": [_build(2)]}, "web": fake.jobs["web"]})
    if mode == "missing":
        assert (
            _deleted(list(sync_jenkins(client, BASE_URL, state, confirm_deleted=lambda *_: True)))
            == []
        )
        assert state["jobs"]["api"]["builds"] == [1, 2]
    else:
        with pytest.raises(RuntimeError):
            list(sync_jenkins(client, BASE_URL, state, confirm_deleted=lambda *_: True))
        assert state == before
