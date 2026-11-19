import os
import hashlib
from typing import List, Dict, Any, Optional
import requests

HABITICA_API_URL = "https://habitica.com/api/v3"

def fetch_habitica_data(
    user_id: str,
    api_token: str,
    task_type: Optional[str] = None,
    api_base_url: str = HABITICA_API_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches tasks and habits from the Habitica API v3.
    """
    http = session or requests.Session()
    headers = {
        "x-api-user": user_id,
        "x-api-key": api_token,
        "x-client": f"{user_id}-cognee-connector",
        "Content-Type": "application/json"
    }

    url = f"{api_base_url.rstrip('/')}/tasks/user"
    params = {}
    if task_type:
        params["type"] = task_type

    response = http.get(url, headers=headers, params=params, timeout=20)
    response.raise_for_status()

    result = response.json()
    if not result.get("success", False):
        raise ValueError(f"Habitica API error: {result.get('message', 'Unknown failure')}")

    return result.get("data", [])

def format_habitica_task_markdown(task: Dict[str, Any]) -> str:
    """
    Renders Habitica task and habit metadata into structured Markdown for Cognee.
    """
    task_id = task.get("id", "unknown")
    text = task.get("text", "Untitled Task")
    notes = task.get("notes", "").strip()
    task_type = task.get("type", "task")
    completed = task.get("completed", False)
    value = task.get("value", 0.0)
    priority = task.get("priority", 1)  # 0.1: Trivial, 1: Easy, 1.5: Medium, 2: Hard
    tags = task.get("tags", [])

    lines = [
        f"# {text}",
        f"- **Task ID**: `{task_id}`",
        f"- **Type**: `{task_type}`",
        f"- **Status**: {'Completed' if completed else 'Pending / Active'}",
        f"- **Value Score**: {value:.2f}",
        f"- **Difficulty Rating**: {priority}",
    ]

    if tags:
        lines.append(f"- **Tags**: {', '.join(str(t) for t in tags)}")

    if notes:
        lines.append("\n## Description & Notes\n")
        lines.append(notes)

    # Habits have positive/negative counters
    if task_type == "habit":
        up = task.get("up", True)
        down = task.get("down", False)
        up_counter = task.get("counterUp", 0)
        down_counter = task.get("counterDown", 0)
        lines.append("\n## Habit Tracker\n")
        lines.append(f"- **Positive Streak**: {up_counter} (Enabled: {up})")
        lines.append(f"- **Negative Count**: {down_counter} (Enabled: {down})")

    # Dailies have streak and checklist
    elif task_type == "daily":
        streak = task.get("streak", 0)
        lines.append(f"- **Current Streak**: {streak} days")
        checklist = task.get("checklist", [])
        if checklist:
            lines.append("\n### Checklist Items:")
            for item in checklist:
                box = "[x]" if item.get("completed") else "[ ]"
                lines.append(f"- {box} {item.get('text', '')}")

    return "\n".join(lines)

def habitica_source(
    user_id: Optional[str] = None,
    api_token: Optional[str] = None,
    task_type: Optional[str] = None,
    api_base_url: str = HABITICA_API_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Cognee data connector for Habitica gamified habits and tasks.
    Returns normalized document entities ready for `cognee.remember()`.
    """
    uid = user_id or os.getenv("HABITICA_USER_ID", "")
    token = api_token or os.getenv("HABITICA_API_KEY", "")

    if not uid or not token:
        raise ValueError("Habitica user_id and api_token must be provided or set via HABITICA_USER_ID and HABITICA_API_KEY.")

    raw_tasks = fetch_habitica_data(
        user_id=uid,
        api_token=token,
        task_type=task_type,
        api_base_url=api_base_url,
        session=session
    )

    documents = []
    for task in raw_tasks:
        task_id = str(task.get("id", ""))
        text = task.get("text", "Untitled")
        content_md = format_habitica_task_markdown(task)

        # Deterministic document ID
        doc_id = hashlib.sha256(f"habitica:{uid}:{task_id}".encode("utf-8")).hexdigest()

        documents.append({
            "id": doc_id,
            "title": f"Habitica Task: {text}",
            "content": content_md,
            "metadata": {
                "source": "habitica",
                "task_id": task_id,
                "type": task.get("type", "task"),
                "completed": task.get("completed", False),
                "priority": task.get("priority", 1),
                "url": f"https://habitica.com/#/tasks/{task_id}"
            }
        })

    return documents
