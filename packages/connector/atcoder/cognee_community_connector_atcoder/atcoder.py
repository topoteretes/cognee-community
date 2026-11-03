"""AtCoder Data-Source Connector for Cognee.

Ingests competitive programming user submissions, problem difficulty models,
and contest performance history from AtCoder and the Kenkoooo AtCoder Problems API.
Resolves issue: cognee#6406.
"""

import os
import time
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional, Iterator, Union
from datetime import datetime, timezone

# Optional dlt import for Cognee ecosystem compatibility
try:
    import dlt
    _has_dlt = True
except ImportError:
    dlt = None
    _has_dlt = False

def _optional_source(func):
    if _has_dlt and hasattr(dlt, "source"):
        return dlt.source(func)
    return func

def _optional_resource(*args, **kwargs):
    def decorator(func):
        if _has_dlt and hasattr(dlt, "resource"):
            return dlt.resource(*args, **kwargs)(func)
        return func
    return decorator


class AtCoderClient:
    """Client for AtCoder contest history and Kenkoooo AtCoder Problems public APIs."""

    KENKOOOO_BASE_URL = "https://kenkoooo.com/atcoder"
    ATCODER_BASE_URL = "https://atcoder.jp"

    def __init__(
        self,
        kenkoooo_base_url: Optional[str] = None,
        atcoder_base_url: Optional[str] = None,
        min_interval_seconds: float = 1.0,
        user_agent: Optional[str] = None
    ):
        self.kenkoooo_base_url = (kenkoooo_base_url or self.KENKOOOO_BASE_URL).rstrip("/")
        self.atcoder_base_url = (atcoder_base_url or self.ATCODER_BASE_URL).rstrip("/")
        self.min_interval = min_interval_seconds
        self.last_request_time = 0.0
        self.user_agent = user_agent or "AtCoderCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)"
        self._cached_problems: Optional[Dict[str, Dict[str, Any]]] = None
        self._cached_problem_models: Optional[Dict[str, Dict[str, Any]]] = None

    def _rate_limit(self):
        """Enforces at least min_interval_seconds between API calls as requested by Kenkoooo."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()

    def _http_get(self, url: str, max_retries: int = 3) -> Any:
        self._rate_limit()
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json"
        }
        req = urllib.request.Request(url, headers=headers)
        last_exc = None

        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                last_exc = e
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_exc = e
                time.sleep(1.0 * (attempt + 1))
                continue

        raise last_exc or RuntimeError(f"Failed to fetch {url}")

    def get_problems_index(self, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Fetches and caches the index of all AtCoder problems."""
        if self._cached_problems is not None and not force_refresh:
            return self._cached_problems

        url = f"{self.kenkoooo_base_url}/resources/problems.json"
        data = self._http_get(url)
        index = {}
        for item in data:
            prob_id = item.get("id")
            if prob_id:
                index[prob_id] = item
        self._cached_problems = index
        return index

    def get_problem_models(self, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Fetches and caches estimated difficulty models for AtCoder problems."""
        if self._cached_problem_models is not None and not force_refresh:
            return self._cached_problem_models

        url = f"{self.kenkoooo_base_url}/resources/problem-models.json"
        data = self._http_get(url)
        self._cached_problem_models = data if isinstance(data, dict) else {}
        return self._cached_problem_models

    def get_user_submissions(
        self,
        handle: str,
        from_second: int = 0,
        max_submissions: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Fetches all submissions for a user with automatic epoch-based pagination."""
        all_submissions: List[Dict[str, Any]] = []
        current_from_second = from_second

        while True:
            encoded_user = urllib.parse.quote(handle.strip())
            url = f"{self.kenkoooo_base_url}/atcoder-api/v3/user/submissions?user={encoded_user}&from_second={current_from_second}"
            chunk = self._http_get(url)

            if not isinstance(chunk, list) or not chunk:
                break

            all_submissions.extend(chunk)

            if max_submissions and len(all_submissions) >= max_submissions:
                all_submissions = all_submissions[:max_submissions]
                break

            # If fewer than 500 returned, we reached the end of the user's submission history
            if len(chunk) < 500:
                break

            # Next page starts 1 second after the highest epoch second in this chunk
            max_epoch = max(sub.get("epoch_second", current_from_second) for sub in chunk)
            if max_epoch <= current_from_second:
                break
            current_from_second = max_epoch + 1

        return all_submissions

    def get_user_contest_history(self, handle: str) -> List[Dict[str, Any]]:
        """Fetches official rated contest history from AtCoder."""
        encoded_user = urllib.parse.quote(handle.strip())
        url = f"{self.atcoder_base_url}/users/{encoded_user}/history/json"
        try:
            data = self._http_get(url)
            if not isinstance(data, list):
                return []
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ValueError(f"AtCoder handle not found: '{handle}'")
            raise

    def fetch_user_data(self, handle: str, max_submissions: Optional[int] = None) -> Dict[str, Any]:
        """Fetches submissions, contest history, and computes structured problem documents."""
        handle_clean = handle.strip()
        submissions = self.get_user_submissions(handle_clean, max_submissions=max_submissions)
        contests = self.get_user_contest_history(handle_clean)

        # Safety check: if user has zero submissions and zero contests, verify handle existence
        if not submissions and not contests:
            raise ValueError(f"No AtCoder profile or submission history found for '{handle_clean}'. Check the handle spelling.")

        problems_index = self.get_problems_index()
        models = self.get_problem_models()

        # Group submissions by problem
        grouped_problems: Dict[str, List[Dict[str, Any]]] = {}
        ac_problem_ids = set()

        for sub in submissions:
            prob_id = sub.get("problem_id", "unknown")
            grouped_problems.setdefault(prob_id, []).append(sub)
            if sub.get("result") == "AC":
                ac_problem_ids.add(prob_id)

        # Synthesize problem-level documents (WA, WA, AC grouped together)
        problem_documents = []
        for prob_id, attempts in grouped_problems.items():
            prob_meta = problems_index.get(prob_id, {})
            model_meta = models.get(prob_id, {})

            title = prob_meta.get("title") or prob_id
            contest_id = prob_meta.get("contest_id") or (attempts[0].get("contest_id") if attempts else "unknown")
            difficulty = model_meta.get("difficulty")
            is_experimental = model_meta.get("is_experimental", False)

            # Determine overall solved status
            is_solved = any(a.get("result") == "AC" for a in attempts)
            verdicts = [a.get("result", "UNKNOWN") for a in attempts]

            # Format attempts summary
            attempts_formatted = []
            for a in attempts:
                sub_time = datetime.fromtimestamp(a.get("epoch_second", 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                attempts_formatted.append({
                    "id": a.get("id"),
                    "verdict": a.get("result"),
                    "language": a.get("language"),
                    "point": a.get("point"),
                    "execution_time_ms": a.get("execution_time"),
                    "submitted_at": sub_time
                })

            problem_url = f"https://atcoder.jp/contests/{contest_id}/tasks/{prob_id}"

            text_summary = (
                f"# Problem: {title} ({contest_id})\n"
                f"- **Platform**: AtCoder\n"
                f"- **Handle**: {handle_clean}\n"
                f"- **Estimated Difficulty**: {difficulty if difficulty is not None else 'Unrated'}"
                f"{' (Experimental)' if is_experimental else ''}\n"
                f"- **Status**: {'SOLVED (AC)' if is_solved else 'ATTEMPTED (UNSOLVED)'}\n"
                f"- **Total Attempts**: {len(attempts)} (Sequence: {' -> '.join(verdicts)})\n"
                f"- **Problem URL**: {problem_url}\n\n"
                f"## Submissions History\n"
            )
            for af in attempts_formatted:
                text_summary += f"- [{af['submitted_at']}] Verdict: **{af['verdict']}** | Language: {af['language']} | Time: {af['execution_time_ms']}ms\n"

            problem_documents.append({
                "problem_id": prob_id,
                "contest_id": contest_id,
                "title": title,
                "difficulty": difficulty,
                "is_solved": is_solved,
                "attempts_count": len(attempts),
                "attempts": attempts_formatted,
                "problem_url": problem_url,
                "text": text_summary
            })

        # Synthesize contest history documents
        contest_documents = []
        for c in contests:
            is_rated = c.get("IsRated", False)
            contest_name = c.get("ContestName", "Unknown Contest")
            perf = c.get("Performance")
            old_rating = c.get("OldRating")
            new_rating = c.get("NewRating")
            place = c.get("Place")
            end_time = c.get("EndTime")

            contest_summary = (
                f"# Contest: {contest_name}\n"
                f"- **Handle**: {handle_clean}\n"
                f"- **Rated**: {'Yes' if is_rated else 'Unrated'}\n"
                f"- **Place**: {place}\n"
                f"- **Performance**: {perf if perf is not None else 'N/A'}\n"
                f"- **Rating Change**: {old_rating} ➔ {new_rating} ({(new_rating - old_rating):+d} if old_rating and new_rating else 0)\n"
                f"- **Date**: {end_time}\n"
            )

            contest_documents.append({
                "contest_name": contest_name,
                "is_rated": is_rated,
                "place": place,
                "performance": perf,
                "old_rating": old_rating,
                "new_rating": new_rating,
                "end_time": end_time,
                "text": contest_summary
            })

        # Synthesize Profile summary document
        latest_rating = contests[-1].get("NewRating", 0) if contests else 0
        max_rating = max((c.get("NewRating", 0) for c in contests), default=0)
        profile_summary = (
            f"# AtCoder User Profile: {handle_clean}\n"
            f"- **Current Rating**: {latest_rating}\n"
            f"- **Peak Rating**: {max_rating}\n"
            f"- **Rated Contests Played**: {sum(1 for c in contests if c.get('IsRated'))}\n"
            f"- **Total Contests Participated**: {len(contests)}\n"
            f"- **Unique Problems Solved (AC)**: {len(ac_problem_ids)}\n"
            f"- **Total Problems Attempted**: {len(grouped_problems)}\n"
            f"- **Total Submissions**: {len(submissions)}\n"
            f"- **Profile URL**: https://atcoder.jp/users/{handle_clean}\n"
        )

        return {
            "handle": handle_clean,
            "profile": {
                "handle": handle_clean,
                "current_rating": latest_rating,
                "max_rating": max_rating,
                "ac_count": len(ac_problem_ids),
                "attempted_count": len(grouped_problems),
                "contests_count": len(contests),
                "submissions_count": len(submissions),
                "text": profile_summary
            },
            "problems": problem_documents,
            "contests": contest_documents
        }


def atcoder_data(
    handles: Optional[Union[str, List[str]]] = None,
    client: Optional[AtCoderClient] = None
) -> Iterator[Dict[str, Any]]:
    """Yields Cognee-compatible documents for AtCoder user profiles, problems, and contests."""
    if handles is None:
        raw_env = os.getenv("ATCODER_HANDLES", "")
        if raw_env:
            handles = [h.strip() for h in raw_env.split(",") if h.strip()]
        else:
            raise ValueError("Must provide 'handles' parameter or set 'ATCODER_HANDLES' environment variable.")

    if isinstance(handles, str):
        handles = [h.strip() for h in handles.split(",") if h.strip()]

    cli = client or AtCoderClient()

    for handle in handles:
        user_pack = cli.fetch_user_data(handle)
        # Yield profile document
        yield {
            "id": f"atcoder_profile_{handle}",
            "type": "atcoder_user_profile",
            "handle": handle,
            "title": f"AtCoder Profile - {handle}",
            "text": user_pack["profile"]["text"],
            "metadata": user_pack["profile"]
        }
        # Yield problem attempt documents
        for p in user_pack["problems"]:
            yield {
                "id": f"atcoder_problem_{handle}_{p['problem_id']}",
                "type": "atcoder_problem_attempts",
                "handle": handle,
                "problem_id": p["problem_id"],
                "contest_id": p["contest_id"],
                "difficulty": p["difficulty"],
                "is_solved": p["is_solved"],
                "title": f"AtCoder Problem: {p['title']} ({handle})",
                "text": p["text"],
                "metadata": p
            }
        # Yield contest documents
        for idx, c in enumerate(user_pack["contests"]):
            yield {
                "id": f"atcoder_contest_{handle}_{idx}",
                "type": "atcoder_contest_history",
                "handle": handle,
                "contest_name": c["contest_name"],
                "title": f"AtCoder Contest: {c['contest_name']} ({handle})",
                "text": c["text"],
                "metadata": c
            }


@_optional_source
def atcoder_source(handles: Optional[Union[str, List[str]]] = None):
    """Cognee / DLT source wrapper for AtCoder data."""
    @_optional_resource(name="atcoder_data", write_disposition="replace")
    def _data():
        yield from atcoder_data(handles=handles)

    return _data()
