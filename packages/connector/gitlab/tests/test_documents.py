import pytest

from cognee_community_connector_gitlab.documents import (
    _issue_to_document,
    _merge_request_to_document,
    _wiki_to_document,
)

PROJECT = {"id": 42, "path_with_namespace": "team/app"}
INSTANCE = "https://gitlab.example/prefix"


def item(**updates):
    return {
        "id": 987,
        "iid": 12,
        "project_id": 42,
        "title": "Login fails",
        "description": "Users cannot log in.",
        **updates,
    }


def test_issue_document_contains_text_and_provenance():
    payload = item(state="opened", author={"username": "alice"}, labels=["bug", "auth"])
    row = _issue_to_document(payload, PROJECT, INSTANCE)
    assert set(row) == {"id", "title", "content", "url"}
    assert row["id"] == "https://gitlab.example/prefix:project:42:issue:987"
    assert row["title"] == "Login fails"
    assert row["url"] == "https://gitlab.example/prefix/team/app/-/issues/12"
    for text in ("Reference: #12", "Author: alice", "Labels: auth, bug", payload["description"]):
        assert text in row["content"]
    assert "web_url" not in payload  # Mapping does not mutate the input.


@pytest.mark.parametrize("mapper", [_issue_to_document, _merge_request_to_document])
def test_nullable_optional_fields(mapper):
    row = mapper(item(description=None, labels=None, author=None), PROJECT, INSTANCE)
    assert "None" not in row["content"]
    assert "Author:" not in row["content"]


def test_merge_request_branches_and_identity():
    payload = item(state="merged", source_branch="fix/login", target_branch="main")
    row = _merge_request_to_document(payload, PROJECT, INSTANCE)
    assert "Reference: !12" in row["content"]
    assert "Source branch: fix/login" in row["content"]
    assert "Target branch: main" in row["content"]
    assert row["id"] != _issue_to_document(payload, PROJECT, INSTANCE)["id"]
    assert row["url"].endswith("/-/merge_requests/12")


def test_identity_stable_across_edits_and_distinct_across_instances():
    original = _issue_to_document(item(), PROJECT, INSTANCE)
    edited = _issue_to_document(item(title="New title", description="New text"), PROJECT, INSTANCE)
    assert original["id"] == edited["id"]
    assert original["content"] != edited["content"]
    assert original["id"] != _issue_to_document(item(), PROJECT, "https://other.example")["id"]
    other_project = {"id": 43, "path_with_namespace": "team/other"}
    assert original["id"] != _issue_to_document(item(project_id=43), other_project, INSTANCE)["id"]


def test_api_web_url_preferred_and_labels_have_stable_order():
    url = "https://gitlab.example/prefix/team/app/-/issues/12"
    first = _issue_to_document(item(web_url=url, labels=["bug", "auth"]), PROJECT, INSTANCE)
    second = _issue_to_document(item(web_url=url, labels=["auth", "bug"]), PROJECT, INSTANCE)
    assert first == second
    assert first["url"] == url


def test_wiki_preserves_markup_and_encodes_nested_slug():
    page = {
        "slug": "guides/setup & run",
        "title": "Setup",
        "content": "= Setup\n",
        "format": "asciidoc",
    }
    row = _wiki_to_document(page, PROJECT, INSTANCE)
    assert row["url"].endswith("/-/wikis/guides/setup%20%26%20run")
    assert row["id"].endswith(":wiki:guides%2Fsetup%20%26%20run")
    assert row["content"].endswith(page["content"])
    assert "Format: asciidoc" in row["content"]
    renamed = _wiki_to_document({**page, "slug": "new-name"}, PROJECT, INSTANCE)
    assert row["id"] != renamed["id"]


def test_empty_wiki_content_and_project_web_url():
    project = {"id": 42, "web_url": "https://gitlab.example/prefix/team/app/"}
    row = _wiki_to_document({"slug": "home", "title": "Home", "content": ""}, project, INSTANCE)
    assert row["url"] == "https://gitlab.example/prefix/team/app/-/wikis/home"


@pytest.mark.parametrize(
    "updates",
    [{"id": None}, {"iid": True}, {"project_id": 99}, {"title": None}, {"description": []}],
)
def test_invalid_issue_payload_is_rejected(updates):
    with pytest.raises((ValueError, TypeError)):
        _issue_to_document(item(**updates), PROJECT, INSTANCE)


def test_missing_wiki_content_is_rejected():
    with pytest.raises(TypeError):
        _wiki_to_document({"slug": "home", "title": "Home"}, PROJECT, INSTANCE)
