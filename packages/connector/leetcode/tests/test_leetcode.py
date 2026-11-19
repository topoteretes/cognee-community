import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_leetcode import (
    LeetCodeConnector,
    fetch_user_recent_submissions,
    fetch_user_profile_stats,
    fetch_question_details,
    format_submission_markdown,
    format_question_markdown,
)
from cognee_community_connector_leetcode.leetcode import get_headers

MOCK_RECENT_SUBMISSIONS = [
    {
        "id": "11223344",
        "title": "Trapping Rain Water",
        "titleSlug": "trapping-rain-water",
        "timestamp": "1730000000"
    }
]

MOCK_QUESTION_DETAILS = {
    "questionId": "42",
    "title": "Trapping Rain Water",
    "titleSlug": "trapping-rain-water",
    "difficulty": "Hard",
    "topicTags": [
        {"name": "Array", "slug": "array"},
        {"name": "Two Pointers", "slug": "two-pointers"},
        {"name": "Dynamic Programming", "slug": "dynamic-programming"}
    ]
}

MOCK_USER_STATS = {
    "username": "tourist",
    "submitStatsGlobal": {
        "acSubmissionNum": [
            {"difficulty": "All", "count": 550},
            {"difficulty": "Easy", "count": 150},
            {"difficulty": "Medium", "count": 280},
            {"difficulty": "Hard", "count": 120}
        ]
    },
    "profile": {
        "ranking": 42,
        "reputation": 999
    }
}

def test_get_headers():
    h1 = get_headers()
    assert h1["Content-Type"] == "application/json"
    assert "Cookie" not in h1

    h2 = get_headers("my_leetcode_session")
    assert h2["Cookie"] == "LEETCODE_SESSION=my_leetcode_session"

def test_format_submission_markdown():
    sub = MOCK_RECENT_SUBMISSIONS[0]
    md = format_submission_markdown(sub, question_details=MOCK_QUESTION_DETAILS, username="tourist")

    assert "# LeetCode Solution: Trapping Rain Water" in md
    assert "**Submission ID:** `11223344`" in md
    assert "**Coder:** `tourist`" in md
    assert "**Difficulty:** HARD" in md
    assert "**Topic Tags:** Array, Two Pointers, Dynamic Programming" in md
    assert "https://leetcode.com/problems/trapping-rain-water/" in md

def test_format_question_markdown():
    md = format_question_markdown(MOCK_QUESTION_DETAILS)
    assert "# LeetCode #42: Trapping Rain Water" in md
    assert "**Difficulty:** HARD" in md
    assert "Two Pointers" in md

@patch("requests.Session.post")
def test_fetch_user_recent_submissions(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {"recentAcSubmissionList": MOCK_RECENT_SUBMISSIONS}}
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    subs = fetch_user_recent_submissions("tourist")
    assert len(subs) == 1
    assert subs[0]["title"] == "Trapping Rain Water"

@patch("requests.Session.post")
def test_fetch_user_profile_stats(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {"matchedUser": MOCK_USER_STATS}}
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    stats = fetch_user_profile_stats("tourist")
    assert stats["username"] == "tourist"
    assert len(stats["submitStatsGlobal"]["acSubmissionNum"]) == 4

@patch("requests.Session.post")
def test_fetch_question_details(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {"question": MOCK_QUESTION_DETAILS}}
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    q = fetch_question_details("trapping-rain-water")
    assert q["questionId"] == "42"
    assert q["difficulty"] == "Hard"

@patch("requests.Session.post")
def test_leetcode_connector_fetch_documents(mock_post):
    resp_subs = MagicMock()
    resp_subs.json.return_value = {"data": {"recentAcSubmissionList": MOCK_RECENT_SUBMISSIONS}}
    resp_subs.raise_for_status = MagicMock()

    resp_q = MagicMock()
    resp_q.json.return_value = {"data": {"question": MOCK_QUESTION_DETAILS}}
    resp_q.raise_for_status = MagicMock()

    mock_post.side_effect = [resp_subs, resp_q]

    connector = LeetCodeConnector(username="tourist", include_problem_details=True)
    docs = connector.fetch_documents()

    assert len(docs) == 1
    doc = docs[0]
    assert doc["metadata"]["source"] == "leetcode"
    assert doc["metadata"]["username"] == "tourist"
    assert doc["metadata"]["difficulty"] == "Hard"
    assert len(doc["id"]) == 64

def test_leetcode_connector_empty_username_raises():
    with pytest.raises(ValueError, match="A valid LeetCode username must be provided"):
        LeetCodeConnector(username="")
