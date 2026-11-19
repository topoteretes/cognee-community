"""Unit and integration test suite for Cognee Gitee connector."""

import os
import json
import pytest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from cognee_community_connector_gitee import (
    GiteeClient,
    GiteeConnector,
    format_gitee_document,
    fetch_gitee_repository_data,
)


class TestGiteeClient:
    """Tests for GiteeClient HTTP and request behaviors."""

    def test_client_init_defaults(self):
        client = GiteeClient(access_token="test_token_123")
        assert client.access_token == "test_token_123"
        assert client.base_url == "https://gitee.com/api/v5"
        headers = client._get_headers()
        assert headers["Authorization"] == "token test_token_123"
        assert "GiteeCogneeConnector" in headers["User-Agent"]

    def test_client_init_env_fallback(self, monkeypatch):
        monkeypatch.setenv("GITEE_ACCESS_TOKEN", "env_token_456")
        client = GiteeClient()
        assert client.access_token == "env_token_456"

    @patch("urllib.request.urlopen")
    def test_request_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([{"id": 1, "name": "repo1"}]).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        client = GiteeClient(access_token="test_token")
        res = client._request("user/repos")
        assert len(res) == 1
        assert res[0]["name"] == "repo1"

    @patch("urllib.request.urlopen")
    def test_request_auth_error_401(self, mock_urlopen):
        mock_error = HTTPError(url="https://gitee.com/api/v5/user/repos", code=401, msg="Unauthorized", hdrs={}, fp=MagicMock())
        mock_error.read = MagicMock(return_value=b"Token invalid")
        mock_urlopen.side_effect = mock_error

        client = GiteeClient(access_token="bad_token")
        with pytest.raises(PermissionError) as exc_info:
            client._request("user/repos")
        assert "authentication failed" in str(exc_info.value)

    @patch("urllib.request.urlopen")
    def test_request_rate_limit_429(self, mock_urlopen):
        mock_error = HTTPError(url="https://gitee.com/api/v5/user/repos", code=429, msg="Too Many Requests", hdrs={}, fp=MagicMock())
        mock_error.read = MagicMock(return_value=b"Rate limit exceeded")
        mock_urlopen.side_effect = mock_error

        client = GiteeClient(access_token="token")
        with pytest.raises(ConnectionRefusedError) as exc_info:
            client._request("user/repos")
        assert "rate limit reached" in str(exc_info.value)

    def test_pagination_helper(self):
        client = GiteeClient(access_token="test")
        mock_fetch = MagicMock()
        # Return page 1 (50 items), page 2 (10 items), page 3 should not be called
        page1 = [{"id": i} for i in range(50)]
        page2 = [{"id": 50 + i} for i in range(10)]
        mock_fetch.side_effect = [page1, page2]

        results = client.paginate(mock_fetch, max_pages=5, per_page=50)
        assert len(results) == 60
        assert mock_fetch.call_count == 2


class TestGiteeFormatting:
    """Tests document generation models for Cognee."""

    def test_format_repository(self):
        repo_data = {
            "id": 101,
            "full_name": "oschina/git-osc",
            "description": "Git-based code hosting",
            "stargazers_count": 500,
            "html_url": "https://gitee.com/oschina/git-osc",
        }
        doc = format_gitee_document("repository", repo_data, "oschina", "git-osc")
        assert doc["metadata"]["source"] == "gitee"
        assert doc["metadata"]["item_type"] == "repository"
        assert "Git-based code hosting" in doc["text"]
        assert doc["write_disposition"] == "replace"
        assert len(doc["data_id"]) == 64 # SHA-256

    def test_format_issue(self):
        issue_data = {
            "number": "I12345",
            "title": "Bug in commit hook",
            "state": "open",
            "user": {"login": "dev_user"},
            "body": "Hooks do not execute properly on Windows.",
            "html_url": "https://gitee.com/oschina/git-osc/issues/I12345",
        }
        doc = format_gitee_document("issue", issue_data, "oschina", "git-osc")
        assert doc["metadata"]["item_type"] == "issue"
        assert "Bug in commit hook" in doc["name"]
        assert "Hooks do not execute" in doc["text"]
        assert doc["metadata"]["raw_id"] == "I12345"

    def test_format_pull_request(self):
        pr_data = {
            "number": 42,
            "title": "Add async support",
            "state": "merged",
            "user": {"login": "contributor"},
            "head": {"ref": "feature-async"},
            "base": {"ref": "master"},
            "body": "Refactors network layer to use asyncio.",
        }
        doc = format_gitee_document("pull_request", pr_data, "oschina", "git-osc")
        assert doc["metadata"]["item_type"] == "pull_request"
        assert "feature-async -> Base: master" in doc["text"]
        assert "Refactors network layer" in doc["text"]

    def test_format_commit(self):
        commit_data = {
            "sha": "abcdef1234567890",
            "commit": {
                "message": "fix: sanitize input strings",
                "author": {"name": "Alice", "date": "2026-11-01T00:00:00Z"},
            },
            "html_url": "https://gitee.com/oschina/git-osc/commit/abcdef1234567890",
        }
        doc = format_gitee_document("commit", commit_data, "oschina", "git-osc")
        assert doc["metadata"]["item_type"] == "commit"
        assert "sanitize input strings" in doc["text"]
        assert "Alice" in doc["text"]


class TestGiteeConnector:
    """Tests end-to-end repository data extraction."""

    def test_fetch_repository_mocked(self):
        mock_client = MagicMock()
        mock_client.get_repo.return_value = {"id": 1, "full_name": "test/repo"}
        mock_client.paginate.side_effect = [
            [{"number": "I1", "title": "Issue 1", "body": "Issue text"}], # issues
            [{"number": 1, "title": "PR 1", "body": "PR text"}],          # pulls
            [{"sha": "c123", "commit": {"message": "Initial commit"}}],   # commits
            [{"tag_name": "v1.0.0", "body": "First release"}],            # releases
        ]

        connector = GiteeConnector(client=mock_client)
        docs = connector.fetch_repository("test", "repo")
        assert len(docs) == 5 # 1 repo + 1 issue + 1 PR + 1 commit + 1 release
        types = [d["metadata"]["item_type"] for d in docs]
        assert "repository" in types
        assert "issue" in types
        assert "pull_request" in types
        assert "commit" in types
        assert "release" in types


class TestGiteeLiveSmoke:
    """Optional smoke test against live public Gitee API v5."""

    def test_public_repo_metadata_smoke(self):
        client = GiteeClient()
        try:
            repo = client.get_repo("oschina", "git-osc")
            if repo and "id" in repo:
                assert repo.get("full_name") == "oschina/git-osc"
        except (ConnectionError, PermissionError, ConnectionRefusedError, FileNotFoundError):
            pytest.skip("Gitee API network unreachable or rate limited in test environment")
