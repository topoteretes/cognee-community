import hashlib
from typing import List, Dict, Any, Optional
import requests
from datetime import datetime, timezone

ATCODER_PROBLEMS_API_BASE = "https://kenkoooo.com/atcoder/atcoder-api/v3"
ATCODER_OFFICIAL_BASE = "https://atcoder.jp"

def get_default_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "cognee-atcoder-connector/0.1.0 (https://github.com/topoteretes/cognee-community)"
    }

def fetch_user_submissions(
    username: str,
    from_second: int = 0,
    api_base_url: str = ATCODER_PROBLEMS_API_BASE,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches up to 500 recent submissions for an AtCoder user.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/user/submissions"
    params = {"user": username, "from_second": from_second}
    
    response = http.get(url, headers=get_default_headers(), params=params, timeout=25)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data
    return []

def fetch_user_contest_history(
    username: str,
    official_base_url: str = ATCODER_OFFICIAL_BASE,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches official contest participation history and rating trajectory for an AtCoder user.
    """
    http = session or requests.Session()
    url = f"{official_base_url.rstrip('/')}/users/{username}/history/json"
    
    response = http.get(url, headers=get_default_headers(), timeout=25)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data
    return []

def format_submission_markdown(submission: Dict[str, Any], username: str) -> str:
    """
    Transforms an AtCoder submission into semantic Markdown for Cognee knowledge graph ingestion.
    """
    sub_id = submission.get("id", "Unknown")
    epoch_second = submission.get("epoch_second", 0)
    time_str = datetime.fromtimestamp(epoch_second, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if epoch_second else "N/A"
    problem_id = submission.get("problem_id", "Unknown")
    contest_id = submission.get("contest_id", "Unknown")
    verdict = submission.get("result", "UNKNOWN")
    language = submission.get("language", "Unknown")
    point = submission.get("point", 0.0)
    execution_time = submission.get("execution_time")
    exec_str = f"{execution_time} ms" if execution_time is not None else "N/A"

    return f"""# AtCoder Submission: {problem_id} ({verdict})

- **User:** {username}
- **Submission ID:** {sub_id}
- **Contest:** {contest_id}
- **Problem:** {problem_id}
- **Result:** {verdict}
- **Score / Points:** {point}
- **Execution Time:** {exec_str}
- **Language:** {language}
- **Submitted At:** {time_str}
- **Submission URL:** https://atcoder.jp/contests/{contest_id}/submissions/{sub_id}
"""

def format_contest_history_markdown(contest: Dict[str, Any], username: str) -> str:
    """
    Transforms contest performance into semantic Markdown.
    """
    contest_screen_name = contest.get("ContestScreenName", "Unknown Contest")
    contest_name = contest.get("ContestName", contest_screen_name)
    place = contest.get("Place", "N/A")
    old_rating = contest.get("OldRating", 0)
    new_rating = contest.get("NewRating", 0)
    perf = contest.get("Performance", "N/A")
    end_time = contest.get("EndTime", "N/A")
    diff = new_rating - old_rating
    diff_sign = f"+{diff}" if diff >= 0 else str(diff)

    return f"""# AtCoder Contest Record: {contest_name}

- **User:** {username}
- **Contest ID:** {contest_screen_name}
- **Date & End Time:** {end_time}
- **Rank / Place:** {place}
- **Performance Rating:** {perf}
- **Old Rating:** {old_rating}
- **New Rating:** {new_rating} ({diff_sign})
- **Contest URL:** https://atcoder.jp/contests/{contest_screen_name}
"""

class AtCoderConnector:
    """
    Cognee Community Data-Source Connector for AtCoder competitive programming platform.
    """
    def __init__(
        self,
        username: str,
        session: Optional[requests.Session] = None
    ):
        if not username or not username.strip():
            raise ValueError("AtCoder username must be provided.")
        self.username = username.strip()
        self.session = session or requests.Session()

    def fetch_documents(self, include_contests: bool = True) -> List[Dict[str, Any]]:
        """
        Extracts submissions and contest performance as Cognee documents.
        """
        documents = []

        # 1. Fetch Submissions
        try:
            submissions = fetch_user_submissions(self.username, session=self.session)
            for sub in submissions:
                sub_id = str(sub.get("id", ""))
                doc_id = hashlib.sha256(f"atcoder_sub_{self.username}_{sub_id}".encode()).hexdigest()
                text = format_submission_markdown(sub, self.username)
                documents.append({
                    "id": doc_id,
                    "text": text,
                    "metadata": {
                        "source": "atcoder",
                        "type": "submission",
                        "username": self.username,
                        "submission_id": sub_id,
                        "problem_id": sub.get("problem_id"),
                        "contest_id": sub.get("contest_id"),
                        "verdict": sub.get("result"),
                        "language": sub.get("language")
                    }
                })
        except Exception as e:
            # gracefully capture or raise
            pass

        # 2. Fetch Contest History
        if include_contests:
            try:
                contests = fetch_user_contest_history(self.username, session=self.session)
                for c in contests:
                    c_id = str(c.get("ContestScreenName", ""))
                    doc_id = hashlib.sha256(f"atcoder_contest_{self.username}_{c_id}".encode()).hexdigest()
                    text = format_contest_history_markdown(c, self.username)
                    documents.append({
                        "id": doc_id,
                        "text": text,
                        "metadata": {
                            "source": "atcoder",
                            "type": "contest",
                            "username": self.username,
                            "contest_id": c_id,
                            "place": c.get("Place"),
                            "performance": c.get("Performance"),
                            "new_rating": c.get("NewRating")
                        }
                    })
            except Exception:
                pass

        return documents
