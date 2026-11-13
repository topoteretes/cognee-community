"""Render Lattice objects as short plain-text documents for cognify.

Every function is pure and deterministic: the same Lattice data always renders
the same text, so cognee's content hash can tell an edit from a no-op. Fields
that change without the text changing (ids, urls, ``lastUpdatedAt``) are left
out of the text for that reason.
"""

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

# A review's reviewer is never rendered, only the direction of the review. Peer
# reviews are often anonymous to the person they are about, and memory can be
# queried by people who could not see the author in Lattice.
UNKNOWN_USER = "an unknown user"


def when(value: Any) -> str | None:
    """A Lattice timestamp (an integer of UTC epoch seconds) as ``YYYY-MM-DD``."""
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        return None
    if value > 10**11:  # milliseconds, in case a field ever carries them
        value /= 1000
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d")


def goal_title(goal: Mapping[str, Any]) -> str:
    return goal.get("name") or f"Goal {goal.get('id')}"


def goal_content(
    goal: Mapping[str, Any],
    owners: Iterable[str],
    parent_name: str | None,
    updates: Iterable[Mapping[str, Any]],
) -> str:
    kind = " ".join(
        part
        for part in (
            _words(goal.get("goalType")),
            "key result" if goal.get("okrType") == "key_result" else "goal",
        )
        if part
    )
    lines = [f"Type: {kind}"]
    names = sorted(set(owners))
    if names:
        lines.append(f"Owners: {', '.join(names)}")
    state = " ".join(
        part
        for part in (
            f"State: {goal['state']}." if goal.get("state") else "",
            f"Status: {_words(goal['status'])}." if goal.get("status") else "",
        )
        if part
    )
    if state:
        lines.append(state)
    if parent_name:
        lines.append(f"Parent goal: {parent_name}")
    dates = [
        f"{label}: {goal[key]}"
        for label, key in (("Start", "startDate"), ("Due", "dueDate"))
        if goal.get(key)
    ]
    for label, key in (("Completed", "completedAt"), ("Archived", "archivedAt")):
        if when(goal.get(key)):
            dates.append(f"{label}: {when(goal[key])}")
    if dates:
        lines.append(". ".join(dates) + ".")
    amount = _amount(goal)
    if amount:
        lines.append(amount)
    if goal.get("priority") is not None:
        lines.append(f"Priority: {goal['priority']}")
    if (goal.get("description") or "").strip():
        lines.append(f"Description: {goal['description'].strip()}")

    entries = [_update_line(update) for update in sorted(updates, key=_update_key)]
    if entries:
        lines.append("Progress updates:")
        lines.extend(entries)
    return "\n".join(lines)


def feedback_title(author: str, targets: Iterable[str]) -> str:
    names = sorted(set(targets))
    return f"Feedback from {author} to {', '.join(names) if names else UNKNOWN_USER}"


def feedback_content(feedback: Mapping[str, Any]) -> str:
    lines = []
    if when(feedback.get("createdAt")):
        lines.append(f"Given: {when(feedback['createdAt'])}")
    lines.append("Shared with the whole company" if feedback.get("isPublic") else "Private")
    values = sorted(
        {v.get("name") for v in _items(feedback.get("associatedValues")) if v.get("name")}
    )
    if values:
        lines.append(f"Company values: {', '.join(values)}")
    lines.append(f"Feedback: {(feedback.get('body') or '').strip()}")
    return "\n".join(lines)


def cycle_title(cycle: Mapping[str, Any]) -> str:
    return f"Review cycle: {cycle.get('name') or cycle.get('id')}"


def cycle_content(cycle: Mapping[str, Any]) -> str:
    lines = []
    if cycle.get("stage"):
        lines.append(f"Stage: {_words(cycle['stage']).capitalize()}")
    for label, key in (
        ("Peer selection started", "peerSelectionStartedAt"),
        ("Reviews started", "reviewsStartedAt"),
        ("Reviews ended", "reviewsEndedAt"),
        ("Calibration ended", "calibrationEndedAt"),
    ):
        if when(cycle.get(key)):
            lines.append(f"{label}: {when(cycle[key])}")
    if cycle.get("calibrationEnabled"):
        lines.append("Calibration: enabled")
    return "\n".join(lines)


def reviewee_title(cycle_name: str, reviewee: str) -> str:
    return f"Review of {reviewee} in {cycle_name}"


def reviewee_content(
    reviews: Iterable[Mapping[str, Any]],
    questions: Mapping[str, Mapping[str, Any]],
) -> str:
    """One person's submitted reviews in one cycle, grouped under each question."""
    lines: list[str] = []
    by_question: dict[str, list[Mapping[str, Any]]] = {}
    for review in sorted(reviews, key=_review_key):
        by_question.setdefault(_ref_id(review.get("questionRevision")), []).append(review)
    for revision_id, group in by_question.items():
        question = questions.get(revision_id) or {}
        if lines:
            lines.append("")
        lines.append(f"Question: {(question.get('body') or revision_id).strip()}")
        for review in group:
            response = _response_text(review.get("response"))
            calibrated = _response_text(review.get("calibratedResponse"))
            label = f"{_words(review.get('reviewType')).capitalize()} review"
            if response:
                lines.append(f"- {label}: {response}")
            if calibrated:
                lines.append(f"- {label}, calibrated: {calibrated}")
    return "\n".join(lines)


def _response_text(response: Mapping[str, Any] | None) -> str:
    if not response:
        return ""
    parts = []
    rating = response.get("preciseRating")
    if rating is None:
        rating = response.get("ratingString") or response.get("rating")
    if rating is not None:
        parts.append(f"rating {_num(rating)}")
    if response.get("choices"):
        parts.append("chose " + ", ".join(response["choices"]))
    if (response.get("comment") or "").strip():
        parts.append(f"comment: {response['comment'].strip()}")
    return "; ".join(parts)


def _amount(goal: Mapping[str, Any]) -> str | None:
    kind = goal.get("amountType")
    if not kind or kind == "Binary":
        return None
    current, target, start = (
        goal.get(k) for k in ("currentAmount", "endingAmount", "startingAmount")
    )
    if current is None and target is None:
        return None
    text = f"Progress: {_num(current)} of {_num(target)} ({kind.lower()})"
    return f"{text}, started at {_num(start)}" if start is not None else text


def _update_line(update: Mapping[str, Any]) -> str:
    parts = [f"- {update.get('status') or 'update'}"]
    if update.get("increment") not in (None, ""):
        parts.append(f"(increment {update['increment']})")
    comment = (update.get("comment") or "").strip()
    return " ".join(parts) + (f": {comment}" if comment else "")


def _update_key(update: Mapping[str, Any]) -> tuple:
    # Lattice numbers updates in the order they were made.
    ident = update.get("id")
    return (0, ident) if isinstance(ident, int | float) else (1, str(update.get("entityId")))


def _review_key(review: Mapping[str, Any]) -> tuple:
    index = review.get("questionIndex")
    return (
        index if isinstance(index, int) else 10**9,
        str(review.get("reviewType")),
        str(review.get("id")),
    )


def _items(listing: Any) -> list[dict]:
    """The ``data`` of a Lattice list object (``{object, hasMore, data}``)."""
    if isinstance(listing, dict):
        return [item for item in listing.get("data") or [] if isinstance(item, dict)]
    return []


def _ref_id(ref: Any) -> str:
    return str(ref.get("id")) if isinstance(ref, dict) and ref.get("id") else ""


def _words(value: Any) -> str:
    """``CollectingReviews`` -> ``collecting reviews``."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", str(value or "")).lower()


def _num(value: Any) -> str:
    if value is None:
        return "?"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else f"{number:g}"
