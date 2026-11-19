"""Hermetic unit tests for the AtCoder Cognee Community Connector."""

import pytest
from unittest.mock import patch, MagicMock
import urllib.error

from cognee_community_connector_atcoder.atcoder import AtCoderClient, atcoder_data

MOCK_PROBLEMS = [
    {"id": "abc300_a", "contest_id": "abc300", "title": "N-choice question"},
    {"id": "abc300_b", "contest_id": "abc300", "title": "Same Map in the RPG World"},
]

MOCK_MODELS = {
    "abc300_a": {"difficulty": 15, "is_experimental": False},
    "abc300_b": {"difficulty": 620, "is_experimental": False},
}

MOCK_SUBMISSIONS = [
    {
        "id": 1001,
        "epoch_second": 1680000000,
        "problem_id": "abc300_a",
        "contest_id": "abc300",
        "user_id": "tourist",
        "language": "C++ 20 (gcc 12.2)",
        "point": 100.0,
        "execution_time": 5,
        "result": "WA"
    },
    {
        "id": 1002,
        "epoch_second": 1680000100,
        "problem_id": "abc300_a",
        "contest_id": "abc300",
        "user_id": "tourist",
        "language": "C++ 20 (gcc 12.2)",
        "point": 100.0,
        "execution_time": 4,
        "result": "AC"
    },
    {
        "id": 1003,
        "epoch_second": 1680000200,
        "problem_id": "abc300_b",
        "contest_id": "abc300",
        "user_id": "tourist",
        "language": "Python (PyPy 3.10-v7.3.12)",
        "point": 0.0,
        "execution_time": 850,
        "result": "TLE"
    }
]

MOCK_CONTESTS = [
    {
        "IsRated": True,
        "ContestName": "AtCoder Beginner Contest 300",
        "Place": 1,
        "OldRating": 3800,
        "NewRating": 3850,
        "Performance": 4200,
        "EndTime": "2023-04-29T22:40:00+09:00"
    }
]

def test_problem_attempts_grouping():
    """Verifies that multiple submissions for a problem are grouped with sequence and stats."""
    client = AtCoderClient(min_interval_seconds=0.0)

    with patch.object(client, "get_problems_index", return_value={"abc300_a": MOCK_PROBLEMS[0], "abc300_b": MOCK_PROBLEMS[1]}), \
         patch.object(client, "get_problem_models", return_value=MOCK_MODELS), \
         patch.object(client, "get_user_submissions", return_value=MOCK_SUBMISSIONS), \
         patch.object(client, "get_user_contest_history", return_value=MOCK_CONTESTS):

        data = client.fetch_user_data("tourist")

        assert data["handle"] == "tourist"
        assert len(data["problems"]) == 2

        # Check abc300_a: should have 2 attempts, solved = True (WA then AC)
        prob_a = next(p for p in data["problems"] if p["problem_id"] == "abc300_a")
        assert prob_a["attempts_count"] == 2
        assert prob_a["is_solved"] is True
        assert prob_a["difficulty"] == 15
        assert "SOLVED (AC)" in prob_a["text"]
        assert "WA -> AC" in prob_a["text"]

        # Check abc300_b: should have 1 attempt, solved = False (TLE)
        prob_b = next(p for p in data["problems"] if p["problem_id"] == "abc300_b")
        assert prob_b["attempts_count"] == 1
        assert prob_b["is_solved"] is False
        assert prob_b["difficulty"] == 620
        assert "ATTEMPTED (UNSOLVED)" in prob_b["text"]

def test_unknown_handle_raises_error():
    """Verifies that non-existent handles raise an error rather than wiping data with empty snapshots."""
    client = AtCoderClient(min_interval_seconds=0.0)

    with patch.object(client, "get_user_submissions", return_value=[]), \
         patch.object(client, "get_user_contest_history", return_value=[]):
        with pytest.raises(ValueError, match="No AtCoder profile or submission history found"):
            client.fetch_user_data("non_existent_fake_handle_12345")

def test_submissions_pagination_by_epoch():
    """Verifies paging by from_second when a chunk returns 500 items."""
    client = AtCoderClient(min_interval_seconds=0.0)

    # First call returns 500 items with max epoch 1000
    chunk1 = [{"id": i, "epoch_second": 500 + i, "problem_id": f"p_{i}"} for i in range(500)]
    # Second call returns 10 items (less than 500, signalling end of feed)
    chunk2 = [{"id": 1000 + i, "epoch_second": 1001 + i, "problem_id": f"p_{1000+i}"} for i in range(10)]

    with patch.object(client, "_http_get", side_effect=[chunk1, chunk2]) as mock_get:
        subs = client.get_user_submissions("tourist")

        assert len(subs) == 510
        assert mock_get.call_count == 2
        # Verify second call URL has updated from_second (max epoch from chunk1 was 999, so next is 1000)
        call2_url = mock_get.call_args_list[1][0][0]
        assert "from_second=1000" in call2_url

def test_retry_and_backoff_on_transient_error():
    """Verifies automatic retry on 429 / 503 HTTP status codes."""
    client = AtCoderClient(min_interval_seconds=0.0)

    error_429 = urllib.error.HTTPError(
        url="https://kenkoooo.com/atcoder/resources/problems.json",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=None
    )

    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"id": "prob_1", "title": "Test"}]'
        mock_resp.__enter__.return_value = mock_resp
        # First call fails with 429, second call succeeds
        mock_open.side_effect = [error_429, mock_resp]

        result = client.get_problems_index()
        assert "prob_1" in result
        assert mock_open.call_count == 2

def test_atcoder_data_generator_yields_cognee_documents():
    """Verifies that atcoder_data yields structured documents compatible with Cognee."""
    mock_pack = {
        "handle": "chokudai",
        "profile": {
            "handle": "chokudai",
            "current_rating": 2900,
            "max_rating": 3000,
            "text": "# AtCoder User Profile: chokudai\n- Current Rating: 2900\n"
        },
        "problems": [
            {
                "problem_id": "abc100_a",
                "contest_id": "abc100",
                "title": "Happy Birthday",
                "difficulty": 100,
                "is_solved": True,
                "text": "# Problem: Happy Birthday\n"
            }
        ],
        "contests": [
            {
                "contest_name": "ABC 100",
                "text": "# Contest: ABC 100\n"
            }
        ]
    }

    mock_client = MagicMock()
    mock_client.fetch_user_data.return_value = mock_pack

    docs = list(atcoder_data(handles=["chokudai"], client=mock_client))
    assert len(docs) == 3
    doc_types = [d["type"] for d in docs]
    assert "atcoder_user_profile" in doc_types
    assert "atcoder_problem_attempts" in doc_types
    assert "atcoder_contest_history" in doc_types

def test_missing_handles_raises_error():
    """Verifies ValueError when neither handles nor ATCODER_HANDLES env var is provided."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="Must provide 'handles' parameter"):
            list(atcoder_data())

def test_contest_history_404_raises_not_found():
    """Verifies clean handle not found error when AtCoder contest history returns 404."""
    client = AtCoderClient(min_interval_seconds=0.0)
    err_404 = urllib.error.HTTPError(
        url="https://atcoder.jp/users/fake_handle/history/json",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=None
    )
    with patch.object(client, "_http_get", side_effect=err_404):
        with pytest.raises(ValueError, match="AtCoder handle not found: 'fake_handle'"):
            client.get_user_contest_history("fake_handle")

