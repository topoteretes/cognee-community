"""
AtCoder Connector for Cognee.
Synchronizes user submissions, contest participation history, and problem difficulty ratings.
"""

from .atcoder import (
    AtCoderConnector,
    fetch_user_submissions,
    fetch_user_contest_history,
    format_submission_markdown,
    format_contest_history_markdown
)

__all__ = [
    "AtCoderConnector",
    "fetch_user_submissions",
    "fetch_user_contest_history",
    "format_submission_markdown",
    "format_contest_history_markdown"
]
