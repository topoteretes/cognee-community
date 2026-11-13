import logging
import time
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

STACK_OVERFLOW_SOURCE_NAME = "stack_overflow"
DOCUMENT_SOURCE_ATTR = "is_document_source"

def stack_overflow_questions(api_key: str | None = dlt.secrets.value, tags: list[str] = None):
    """Returns the Stack Overflow dlt source."""
    if tags is None:
        tags = []

    @dlt.resource(name="stack_overflow_items")
    def _stack_overflow_resource():
        if not api_key:
            logger.warning("No Stack Overflow API key provided. Rate limits will be very strict.")
        if not tags:
            logger.warning("No tags provided. This may exhaust your quota quickly.")

        state = dlt.current.source_state()
        last_sync = state.setdefault("last_sync_date", 0)

        url = "https://api.stackexchange.com/2.3/questions"
        params = {
            "site": "stackoverflow",
            "order": "desc",
            "sort": "activity",
            "filter": "withbody",  # Returns body for questions
            "pagesize": 100,
            "fromdate": last_sync
        }
        if api_key:
            params["key"] = api_key
        if tags:
            params["tagged"] = ";".join(tags)

        max_creation_date = last_sync

        page = 1
        has_more = True
        while has_more:
            params["page"] = page
            response = requests.get(url, params=params)
            
            # Stack Exchange specific rate limiting handling
            if response.status_code == 429:
                logger.warning("Rate limit hit.")
                # We should backoff, but for simplicity in this connector we raise
                response.raise_for_status()

            response.raise_for_status()
            data = response.json()

            if "backoff" in data:
                time.sleep(data["backoff"])

            for item in data.get("items", []):
                creation_date = item.get("creation_date", 0)
                last_activity_date = item.get("last_activity_date", 0)
                if last_activity_date > max_creation_date:
                    max_creation_date = last_activity_date

                yield _question_to_row(item)
                
                # Fetch answers for this question if any
                answer_count = item.get("answer_count", 0)
                if answer_count > 0:
                    yield from _fetch_answers(item["question_id"], api_key)

            has_more = data.get("has_more", False)
            page += 1

        state["last_sync_date"] = max_creation_date

    def _fetch_answers(question_id, api_key):
        ans_url = f"https://api.stackexchange.com/2.3/questions/{question_id}/answers"
        ans_params = {
            "site": "stackoverflow",
            "filter": "withbody",
            "pagesize": 100
        }
        if api_key:
            ans_params["key"] = api_key

        ans_response = requests.get(ans_url, params=ans_params)
        if ans_response.status_code == 200:
            ans_data = ans_response.json()
            if "backoff" in ans_data:
                time.sleep(ans_data["backoff"])
            for ans in ans_data.get("items", []):
                yield _answer_to_row(ans)

    @dlt.source(name=STACK_OVERFLOW_SOURCE_NAME)
    def _stack_overflow():
        return _stack_overflow_resource()

    source = _stack_overflow()
    setattr(source, DOCUMENT_SOURCE_ATTR, STACK_OVERFLOW_SOURCE_NAME)
    return source

def _question_to_row(item: dict) -> dict:
    title = item.get("title", "Untitled Question")
    body = item.get("body_markdown", item.get("body", ""))
    return {
        "id": f"question_{item.get('question_id')}",
        "title": title,
        "content": f"Question: {title}\\n\\n{body}",
        "url": item.get("link", "")
    }

def _answer_to_row(item: dict) -> dict:
    body = item.get("body_markdown", item.get("body", ""))
    is_accepted = " (Accepted)" if item.get("is_accepted") else ""
    return {
        "id": f"answer_{item.get('answer_id')}",
        "title": f"Answer to Question {item.get('question_id')}{is_accepted}",
        "content": f"Answer{is_accepted}:\\n\\n{body}",
        "url": f"https://stackoverflow.com/a/{item.get('answer_id')}"
    }
