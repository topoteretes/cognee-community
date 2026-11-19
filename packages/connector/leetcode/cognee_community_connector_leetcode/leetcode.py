import os
import hashlib
from typing import List, Dict, Any, Optional
import requests

LEETCODE_GRAPHQL_ENDPOINT = "https://leetcode.com/graphql"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

RECENT_SUBMISSIONS_QUERY = """
query recentAcSubmissions($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    id
    title
    titleSlug
    timestamp
  }
}
"""

USER_STATS_QUERY = """
query userProfileStats($username: String!) {
  matchedUser(username: $username) {
    username
    submitStatsGlobal {
      acSubmissionNum {
        difficulty
        count
      }
    }
    profile {
      ranking
      reputation
    }
  }
}
"""

QUESTION_DETAILS_QUERY = """
query questionData($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionId
    title
    titleSlug
    difficulty
    topicTags {
      name
      slug
    }
  }
}
"""

def get_headers(session_cookie: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com",
        "User-Agent": USER_AGENT,
    }
    if session_cookie:
        headers["Cookie"] = f"LEETCODE_SESSION={session_cookie.strip()}"
    return headers

def fetch_user_recent_submissions(
    username: str,
    limit: int = 20,
    session_cookie: Optional[str] = None,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches recent accepted submissions for a given LeetCode username via public GraphQL API.
    """
    http = session or requests.Session()
    payload = {
        "query": RECENT_SUBMISSIONS_QUERY,
        "variables": {
            "username": username,
            "limit": limit
        }
    }
    resp = http.post(LEETCODE_GRAPHQL_ENDPOINT, json=payload, headers=get_headers(session_cookie), timeout=20)
    resp.raise_for_status()

    data = resp.json()
    return data.get("data", {}).get("recentAcSubmissionList", [])

def fetch_user_profile_stats(
    username: str,
    session_cookie: Optional[str] = None,
    session: Optional[requests.Session] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetches aggregate algorithmic problem-solving stats for a LeetCode user.
    """
    http = session or requests.Session()
    payload = {
        "query": USER_STATS_QUERY,
        "variables": {
            "username": username
        }
    }
    resp = http.post(LEETCODE_GRAPHQL_ENDPOINT, json=payload, headers=get_headers(session_cookie), timeout=20)
    resp.raise_for_status()

    data = resp.json()
    return data.get("data", {}).get("matchedUser")

def fetch_question_details(
    title_slug: str,
    session: Optional[requests.Session] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetches topic tags, problem ID, and difficulty level for a specific question slug.
    """
    http = session or requests.Session()
    payload = {
        "query": QUESTION_DETAILS_QUERY,
        "variables": {
            "titleSlug": title_slug
        }
    }
    resp = http.post(LEETCODE_GRAPHQL_ENDPOINT, json=payload, headers=get_headers(), timeout=20)
    resp.raise_for_status()

    data = resp.json()
    return data.get("data", {}).get("question")

def format_submission_markdown(
    submission: Dict[str, Any],
    question_details: Optional[Dict[str, Any]] = None,
    username: str = ""
) -> str:
    """
    Formats a LeetCode submission into semantic Markdown for Cognee knowledge graph extraction.
    """
    sub_id = submission.get("id", "N/A")
    title = submission.get("title", "Untitled Problem")
    slug = submission.get("titleSlug", "")
    timestamp = submission.get("timestamp", "N/A")
    leetcode_url = f"https://leetcode.com/problems/{slug}/" if slug else ""

    difficulty = "Unknown"
    tags_str = "None"
    if question_details:
        difficulty = question_details.get("difficulty", "Unknown").upper()
        raw_tags = question_details.get("topicTags", [])
        tags = [t.get("name") for t in raw_tags if t.get("name")]
        if tags:
            tags_str = ", ".join(tags)

    lines = [
        f"# LeetCode Solution: {title}",
        "",
        f"- **Submission ID:** `{sub_id}`",
        f"- **Coder:** `{username}`",
        f"- **Difficulty:** {difficulty}",
        f"- **Topic Tags:** {tags_str}",
        f"- **Status:** Accepted (AC)",
        f"- **Solved At:** {timestamp}",
    ]
    if leetcode_url:
        lines.append(f"- **Problem URL:** [{title}]({leetcode_url})")

    lines.append("")
    lines.append("## Algorithmic Context")
    lines.append(f"Problem '{title}' classified as {difficulty} difficulty testing patterns in: {tags_str}.")
    lines.append("Ingested into Cognee Developer Graph for algorithmic pattern association and interview prep memory.")

    return "\n".join(lines)

def format_question_markdown(question: Dict[str, Any]) -> str:
    """
    Formats a LeetCode question catalog entry.
    """
    q_id = question.get("questionId", "N/A")
    title = question.get("title", "Untitled Question")
    slug = question.get("titleSlug", "")
    difficulty = question.get("difficulty", "Medium").upper()
    tags = [t.get("name") for t in question.get("topicTags", []) if t.get("name")]
    tags_str = ", ".join(tags) if tags else "General Algorithms"

    lines = [
        f"# LeetCode #{q_id}: {title}",
        "",
        f"- **Difficulty:** {difficulty}",
        f"- **Topic Tags:** {tags_str}",
        f"- **Problem URL:** https://leetcode.com/problems/{slug}/",
        "",
        "## Algorithmic Concepts",
        f"This problem focuses on foundational algorithms: {tags_str}."
    ]
    return "\n".join(lines)

class LeetCodeConnector:
    """
    Cognee data connector for LeetCode problem solving, submissions, and developer skills graph.
    """
    def __init__(
        self,
        username: str,
        session_cookie: Optional[str] = None,
        include_problem_details: bool = True
    ):
        if not username or not username.strip():
            raise ValueError("A valid LeetCode username must be provided.")

        self.username = username.strip()
        self.session_cookie = session_cookie or os.environ.get("LEETCODE_SESSION", "")
        self.include_problem_details = include_problem_details
        self.session = requests.Session()

    def fetch_documents(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Fetches solved submissions and transforms them into structured Cognee document nodes.
        """
        documents: List[Dict[str, Any]] = []

        # 1. Fetch recent accepted submissions
        submissions = fetch_user_recent_submissions(
            username=self.username,
            limit=limit,
            session_cookie=self.session_cookie,
            session=self.session
        )

        # 2. Iterate and optionally enrich with question metadata
        for sub in submissions:
            slug = sub.get("titleSlug", "")
            q_details = None
            if self.include_problem_details and slug:
                try:
                    q_details = fetch_question_details(slug, session=self.session)
                except Exception:
                    pass

            text = format_submission_markdown(sub, question_details=q_details, username=self.username)
            sub_id = sub.get("id") or sub.get("titleSlug") or ""
            doc_id = hashlib.sha256(f"leetcode_{self.username}_{sub_id}".encode("utf-8")).hexdigest()

            documents.append({
                "id": doc_id,
                "text": text,
                "metadata": {
                    "source": "leetcode",
                    "type": "submission",
                    "username": self.username,
                    "submission_id": sub.get("id"),
                    "title": sub.get("title"),
                    "title_slug": slug,
                    "difficulty": q_details.get("difficulty") if q_details else "Unknown",
                    "timestamp": sub.get("timestamp")
                }
            })

        return documents
