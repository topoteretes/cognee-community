"""Offline unit tests for the ReadMe connector's sync state machine."""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import quote

import pytest

from cognee_community_connector_readme.readme import (
    README_SOURCE_NAME,
    _document_id,
    readme_source,
    sync_readme,
)


def _category(title, updated_at="2026-01-01T00:00:00Z"):
    return {"title": title, "updated_at": updated_at, "uri": f"/categories/{title}"}


def _guide(slug, title, updated_at="2026-01-01T00:00:00Z", body="guide body"):
    return {
        "slug": slug,
        "title": title,
        "updated_at": updated_at,
        "content": {"body": body},
        "uri": f"/branches/stable/guides/{slug}",
    }


def _changelog(slug, title, updated_at="2026-01-01T00:00:00Z", body="change body"):
    return {
        "slug": slug,
        "title": title,
        "updated_at": updated_at,
        "content": {"body": body},
        "uri": f"/changelogs/{slug}",
    }


class FakeReadMeClient:
    """In-memory v2 API fixture with list summaries and full guide reads."""

    def __init__(self, categories, guides_by_category, changelogs):
        self.categories = categories
        self.guides_by_category = guides_by_category
        self.changelogs = changelogs
        self.collection_calls = []
        self.get_calls = []

    def iter_collection(self, path, params=None):
        self.collection_calls.append(path)
        if path.endswith("/categories/guides"):
            return iter(deepcopy(self.categories))
        if path == "/changelogs":
            return iter(deepcopy(self.changelogs))
        for title, guides in self.guides_by_category.items():
            suffix = f"/categories/guides/{quote(title, safe='')}/pages"
            if path.endswith(suffix):
                branch = path.removeprefix("/branches/").split("/", maxsplit=1)[0]
                # Listings deliberately omit markdown bodies; sync_readme must
                # fetch each changed guide via its URI.
                return iter(
                    [
                        {
                            **{
                                key: value
                                for key, value in guide.items()
                                if key not in {"content", "uri"}
                            },
                            "uri": f"/branches/{branch}/guides/{guide['slug']}",
                        }
                        for guide in deepcopy(guides)
                    ]
                )
        raise AssertionError(f"unexpected collection path: {path}")

    def get(self, path):
        self.get_calls.append(path)
        for guides in self.guides_by_category.values():
            for guide in guides:
                if path.endswith(f"/guides/{guide['slug']}"):
                    resource = deepcopy(guide)
                    resource["uri"] = path
                    return resource
        raise AssertionError(f"unexpected resource path: {path}")


def _client():
    return FakeReadMeClient(
        [_category("Getting started"), _category("Reference")],
        {
            "Getting started": [_guide("welcome", "Welcome", body="Start here")],
            "Reference": [_guide("authentication", "Authentication", body="Use a token")],
        },
        [_changelog("launch", "Launch", body="Initial release")],
    )


def test_first_sync_ingests_categories_guides_and_changelog():
    state = {}
    client = _client()

    rows = list(sync_readme(client, state))

    assert {row["kind"] for row in rows} == {"category", "guide", "changelog"}
    assert {row["id"] for row in rows} == {
        _document_id("stable", "category", "Getting started"),
        _document_id("stable", "category", "Reference"),
        _document_id("stable", "guide", "welcome"),
        _document_id("stable", "guide", "authentication"),
        _document_id("global", "changelog", "launch"),
    }
    assert all(row["_deleted"] is False for row in rows)
    assert "Start here" in next(row["content"] for row in rows if row["id"].endswith(":welcome"))
    assert len(client.get_calls) == 2
    assert len(state["revisions"]) == 5


def test_incremental_sync_skips_unchanged_guide_body_fetches():
    state = {}
    list(sync_readme(_client(), state))
    client = _client()

    assert list(sync_readme(client, state)) == []
    assert client.get_calls == []


def test_incremental_sync_fetches_only_changed_guide():
    state = {}
    list(sync_readme(_client(), state))
    client = _client()
    client.guides_by_category["Reference"][0]["updated_at"] = "2026-02-01T00:00:00Z"
    client.guides_by_category["Reference"][0]["content"] = {"body": "Use an updated token"}

    rows = list(sync_readme(client, state))

    assert [row["id"] for row in rows] == [_document_id("stable", "guide", "authentication")]
    assert client.get_calls == ["/branches/stable/guides/authentication"]
    assert "updated token" in rows[0]["content"]


def test_moving_a_guide_to_another_category_reloads_metadata():
    state = {}
    list(sync_readme(_client(), state))
    client = _client()
    moved = client.guides_by_category["Reference"].pop()
    client.guides_by_category["Getting started"].append(moved)

    rows = list(sync_readme(client, state))

    assert [row["id"] for row in rows] == [_document_id("stable", "guide", "authentication")]
    assert rows[0]["category"] == "Getting started"


def test_deleted_source_record_emits_hard_delete_tombstone():
    state = {}
    list(sync_readme(_client(), state))
    client = _client()
    client.guides_by_category["Reference"] = []

    rows = list(sync_readme(client, state))

    assert rows == [{"id": _document_id("stable", "guide", "authentication"), "_deleted": True}]
    assert _document_id("stable", "guide", "authentication") not in state["revisions"]


def test_category_selection_and_branch_scoping_prevent_version_duplicates():
    state = {}
    rows = list(sync_readme(_client(), state, branch="v2.0", category_titles=["Getting started"]))

    assert {row["id"] for row in rows} == {
        _document_id("v2.0", "category", "Getting started"),
        _document_id("v2.0", "guide", "welcome"),
        _document_id("global", "changelog", "launch"),
    }


def test_empty_sweep_never_mass_deletes_known_records():
    state = {"revisions": {_document_id("stable", "guide", "welcome"): "2026-01-01T00:00:00Z"}}
    client = FakeReadMeClient([], {}, [])

    assert list(sync_readme(client, state, include_changelog=False)) == []
    assert state["revisions"] == {
        _document_id("stable", "guide", "welcome"): "2026-01-01T00:00:00Z"
    }


def test_source_declares_document_mode_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = readme_source(client=_client())
    assert document_source_tag(source) == README_SOURCE_NAME


def test_dlt_merge_hard_delete_removes_vanished_guide(tmp_path):
    """The tombstone must remove the destination row, not merely be yielded."""
    dlt = pytest.importorskip("dlt")
    first_client = _client()
    pipeline = dlt.pipeline(
        pipeline_name="readme_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'readme.db'}"),
        dataset_name="readme_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(readme_source(client=first_client, include_changelog=False))

    second_client = _client()
    second_client.guides_by_category["Reference"] = []
    pipeline.run(readme_source(client=second_client, include_changelog=False))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM readme_documents ORDER BY id") as cursor,
    ):
        ids = [row[0] for row in cursor.fetchall()]

    assert _document_id("stable", "guide", "authentication") not in ids
    assert _document_id("stable", "guide", "welcome") in ids
