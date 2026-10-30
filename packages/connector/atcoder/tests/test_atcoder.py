import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_atcoder import (
    AtCoderConnector,
    fetch_user_submissions,
    fetch_user_contest_history,
    format_submission_markdown,
    format_contest_history_markdown
)

MOCK_SUBMISSIONS = [
    {
        "id": 1234567,
        "epoch_second": 1700000000,
        "problem_id": "abc350_c",
        "contest_id": "abc350",
        "user_id": "tourist",
        "language": "C++ 20 (gcc 12.2)",
        "point": 300.0,
        "length": 1540,
        "result": "AC",
        "execution_time": 45
    },
    {
        "id": 1234568,
        "epoch_second": 1700000500,
        "problem_id": "abc350_d",
        "contest_id": "abc350",
        "user_id": "tourist",
        "language": "Python (PyPy 3.10)",
        "point": 0.0,
        "length": 890,
        "result": "WA",
        "execution_time": 120
    }
]

MOCK_CONTESTS = [
    {
        "IsRated": True,
        "Place": 12,
        "OldRating": 2750,
        "NewRating": 2810,
        "Performance": 3120,
        "ContestName": "AtCoder Beginner Contest 350",
        "ContestScreenName": "abc350.contest.atcoder.jp",
        "EndTime": "2024-04-20T22:40:00+09:00"
    }
]

def test_format_submission_markdown():
    sub = MOCK_SUBMISSIONS[0]
    md = format_submission_markdown(sub, "tourist")
    assert "# AtCoder Submission: abc350_c (AC)" in md
    assert "- **User:** tourist" in md
    assert "- **Result:** AC" in md
    assert "- **Execution Time:** 45 ms" in md
    assert "https://atcoder.jp/contests/abc350/submissions/1234567" in md

def test_format_contest_history_markdown():
    contest = MOCK_CONTESTS[0]
    md = format_contest_history_markdown(contest, "tourist")
    assert "# AtCoder Contest Record: AtCoder Beginner Contest 350" in md
    assert "- **Old Rating:** 2750" in md
    assert "- **New Rating:** 2810 (+60)" in md
    assert "- **Rank / Place:** 12" in md

@patch("requests.Session.get")
def test_fetch_user_submissions(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = MOCK_SUBMISSIONS
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    subs = fetch_user_submissions("tourist")
    assert len(subs) == 2
    assert subs[0]["problem_id"] == "abc350_c"
    assert subs[1]["result"] == "WA"

@patch("requests.Session.get")
def test_fetch_user_contest_history(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = MOCK_CONTESTS
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    history = fetch_user_contest_history("tourist")
    assert len(history) == 1
    assert history[0]["Place"] == 12
    assert history[0]["Performance"] == 3120

@patch("cognee_community_connector_atcoder.atcoder.fetch_user_submissions")
@patch("cognee_community_connector_atcoder.atcoder.fetch_user_contest_history")
def test_atcoder_connector_fetch_documents(mock_contests, mock_subs):
    mock_subs.return_value = MOCK_SUBMISSIONS
    mock_contests.return_value = MOCK_CONTESTS

    connector = AtCoderConnector(username="tourist")
    docs = connector.fetch_documents(include_contests=True)

    assert len(docs) == 3
    sub_docs = [d for d in docs if d["metadata"]["type"] == "submission"]
    contest_docs = [d for d in docs if d["metadata"]["type"] == "contest"]

    assert len(sub_docs) == 2
    assert len(contest_docs) == 1
    assert sub_docs[0]["metadata"]["verdict"] == "AC"
    assert contest_docs[0]["metadata"]["place"] == 12

def test_atcoder_connector_invalid_user():
    with pytest.raises(ValueError):
        AtCoderConnector(username="")
