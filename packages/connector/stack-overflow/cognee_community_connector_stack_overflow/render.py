"""Solution-first document renderer for Stack Overflow Q&A.

Converts a Stack Exchange API question dict (with embedded answers) into a
deterministic Markdown document. The accepted answer always appears first,
followed by other answers sorted by score descending.

HTML is converted to Markdown via html2text so code blocks are preserved.
Timestamps are rendered into content but are NOT included in the content
hash — so a question whose answers have only timestamp-level changes (e.g.
last_activity_date ticked) is not re-cognified.
"""

from __future__ import annotations

import hashlib
import datetime
from typing import Any

try:
    import html2text as _h2t

    _converter = _h2t.HTML2Text()
    _converter.ignore_links = False
    _converter.body_width = 0  # no line-wrapping

    def _html_to_md(html: str) -> str:
        return _converter.handle(html or "").strip()

except ImportError:
    import re

    def _html_to_md(html: str) -> str:  # type: ignore[misc]
        """Fallback: strip HTML tags."""
        return re.sub(r"<[^>]+>", "", html or "").strip()


def _ts(unix: int | None) -> str:
    if unix is None:
        return "unknown"
    return datetime.datetime.fromtimestamp(unix, datetime.timezone.utc).strftime("%Y-%m-%d")


def render_question(question: dict[str, Any]) -> dict[str, Any]:
    """Render a Stack Exchange question dict to a cognee document row.

    Returns a dict with keys: id, title, content, url, metadata.
    The ``id`` field is ``stackoverflow:question:{question_id}`` so it is
    namespaced and stable across runs.
    """
    qid: int = question["question_id"]
    title: str = question.get("title", f"Question #{qid}")
    link: str = question.get("link", f"https://stackoverflow.com/q/{qid}")
    tags: list[str] = question.get("tags", [])
    score: int = question.get("score", 0)
    asked_ts: int | None = question.get("creation_date")
    activity_ts: int | None = question.get("last_activity_date")
    body_html: str = question.get("body", "")

    answers: list[dict[str, Any]] = question.get("answers", [])
    accepted_id: int | None = question.get("accepted_answer_id")

    accepted: dict[str, Any] | None = None
    others: list[dict[str, Any]] = []
    for ans in answers:
        if ans.get("answer_id") == accepted_id:
            accepted = ans
        else:
            others.append(ans)
    others.sort(key=lambda a: a.get("score", 0), reverse=True)

    # ---- Build content (deterministic; timestamps NOT in hash body) --------
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append(f"\n**Tags:** {', '.join(tags)}")
    lines.append(f"**Score:** {score}  |  **Link:** {link}")
    lines.append(f"**Asked:** {_ts(asked_ts)}  |  **Last activity:** {_ts(activity_ts)}")

    lines.append("\n## Problem\n")
    lines.append(_html_to_md(body_html))

    if accepted:
        acc_score = accepted.get("score", 0)
        acc_author = (
            accepted.get("owner", {}).get("display_name", "unknown")
        )
        acc_link = accepted.get("link", "")
        lines.append(f"\n## Accepted Solution (score {acc_score}, by {acc_author})\n")
        lines.append(f"[Link]({acc_link})\n")
        lines.append(_html_to_md(accepted.get("body", "")))

    if others:
        lines.append("\n## Other Answers (by score)\n")
        for ans in others:
            a_score = ans.get("score", 0)
            a_author = ans.get("owner", {}).get("display_name", "unknown")
            a_link = ans.get("link", "")
            lines.append(f"### Answer — score {a_score}, by {a_author}\n")
            lines.append(f"[Link]({a_link})\n")
            lines.append(_html_to_md(ans.get("body", "")))
            lines.append("")

    lines.append(
        "\n---\n*Content from Stack Overflow is licensed under CC BY-SA 4.0. "
        "Attribution: Stack Overflow contributors.*"
    )

    content = "\n".join(lines)

    # ---- Stable content hash (excludes timestamps) -------------------------
    hash_body = f"{qid}|{title}|{_html_to_md(body_html)}"
    if accepted:
        hash_body += "|accepted:" + _html_to_md(accepted.get("body", ""))
    for ans in others:
        hash_body += "|ans:" + _html_to_md(ans.get("body", ""))
    data_id = hashlib.sha256(hash_body.encode()).hexdigest()

    metadata = {
        "question_id": qid,
        "tags": tags,
        "score": score,
        "question_created": _ts(asked_ts),
        "last_activity": _ts(activity_ts),
        "accepted_answer_id": accepted_id,
        "answer_count": len(answers),
        "link": link,
        "source": "stackoverflow",
    }

    return {
        "id": f"stackoverflow:question:{qid}",
        "title": title,
        "content": content,
        "url": link,
        "data_id": data_id,
        "metadata": metadata,
    }