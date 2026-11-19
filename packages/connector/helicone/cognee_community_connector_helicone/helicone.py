import aiohttp
import asyncio
from typing import List, Dict, Any, Optional

class HeliconeConnector:
    """
    Helicone LLM Observability & Prompt Management Connector for Cognee.
    
    Ingests versioned prompt templates, curated evaluation datasets, evaluators,
    and user feedback traces from Helicone REST API into Cognee memory graphs.
    """

    DEFAULT_BASE_URL = "https://api.helicone.ai"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        fetch_prompts_flag: bool = True,
        fetch_datasets_flag: bool = True,
        fetch_evaluators_flag: bool = True,
        fetch_feedback_traces_flag: bool = True,
        max_records: int = 50,
    ):
        """
        Initialize HeliconeConnector.

        Args:
            api_key: Helicone Bearer API Key.
            base_url: Base URL (default: https://api.helicone.ai or https://api.eu.helicone.ai).
            fetch_prompts_flag: Ingest versioned prompt templates.
            fetch_datasets_flag: Ingest curated evaluation datasets.
            fetch_evaluators_flag: Ingest evaluator definitions and scores.
            fetch_feedback_traces_flag: Ingest curated feedback-scored LLM traces.
            max_records: Limit per category to guard against proxy log flooding.
        """
        if not api_key:
            raise ValueError("Helicone API key must be provided.")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.fetch_prompts_flag = fetch_prompts_flag
        self.fetch_datasets_flag = fetch_datasets_flag
        self.fetch_evaluators_flag = fetch_evaluators_flag
        self.fetch_feedback_traces_flag = fetch_feedback_traces_flag
        self.max_records = max(1, min(max_records, 200))

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-helicone/0.1.0",
        }

    async def fetch_prompts(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch versioned prompt templates and model configurations."""
        if not self.fetch_prompts_flag:
            return []

        url = f"{self.base_url}/v1/prompt/query"
        payload = {"limit": self.max_records}
        try:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("data", data.get("prompts", data if isinstance(data, list) else []))
                prompts = []
                for p in items:
                    p_id = p.get("id") or p.get("user_defined_id", "unnamed_prompt")
                    model = p.get("model", "unknown_model")
                    version = p.get("version", 1)
                    template = p.get("prompt_text") or p.get("template") or "No prompt template content."
                    created_at = p.get("created_at", "unknown")

                    clean_text = f"""# Helicone Prompt Template: {p_id} (v{version})
Model: {model}
Created At: {created_at}

## Prompt Content
```
{template}
```
""".strip()
                    prompts.append({
                        "source_id": f"helicone:prompt:{p_id}:{version}",
                        "prompt_id": p_id,
                        "model": model,
                        "version": version,
                        "template": template,
                        "clean_text": clean_text,
                    })
                return prompts
        except Exception:
            return []

    async def fetch_datasets(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch curated evaluation datasets and test benchmarks."""
        if not self.fetch_datasets_flag:
            return []

        url = f"{self.base_url}/v1/helicone-dataset/query"
        payload = {"limit": self.max_records}
        try:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("data", data.get("datasets", data if isinstance(data, list) else []))
                datasets = []
                for d in items:
                    d_id = d.get("id", "unnamed_dataset")
                    name = d.get("name", d_id)
                    desc = d.get("description", "Evaluation dataset benchmark.")
                    rows_count = d.get("rows_count") or d.get("count", 0)

                    clean_text = f"""# Helicone Evaluation Dataset: {name} (ID: {d_id})
Total Test Cases: {rows_count}

## Description
{desc}
""".strip()
                    datasets.append({
                        "source_id": f"helicone:dataset:{d_id}",
                        "dataset_id": d_id,
                        "name": name,
                        "rows_count": rows_count,
                        "clean_text": clean_text,
                    })
                return datasets
        except Exception:
            return []

    async def fetch_evaluators(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch evaluator definitions (relevance, hallucination, custom assertions)."""
        if not self.fetch_evaluators_flag:
            return []

        url = f"{self.base_url}/v1/evaluator/query"
        payload = {"limit": self.max_records}
        try:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("data", data.get("evaluators", data if isinstance(data, list) else []))
                evaluators = []
                for ev in items:
                    ev_id = ev.get("id", "unnamed_evaluator")
                    name = ev.get("name", ev_id)
                    eval_type = ev.get("eval_type") or ev.get("type", "llm_as_judge")
                    scoring_type = ev.get("scoring_type", "boolean")

                    clean_text = f"""# Helicone Evaluator: {name} (ID: {ev_id})
Type: {eval_type}
Scoring Mode: {scoring_type}
""".strip()
                    evaluators.append({
                        "source_id": f"helicone:evaluator:{ev_id}",
                        "evaluator_id": ev_id,
                        "name": name,
                        "type": eval_type,
                        "scoring_type": scoring_type,
                        "clean_text": clean_text,
                    })
                return evaluators
        except Exception:
            return []

    async def fetch_feedback_traces(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch curated feedback-scored LLM traces (positive/negative ratings)."""
        if not self.fetch_feedback_traces_flag:
            return []

        url = f"{self.base_url}/v1/feedback/query"
        payload = {"limit": self.max_records}
        try:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("data", data.get("feedbacks", data if isinstance(data, list) else []))
                feedbacks = []
                for fb in items:
                    fb_id = fb.get("id", "unnamed_feedback")
                    rating = "Positive (Thumbs Up)" if fb.get("rating") is True else "Negative (Thumbs Down)"
                    comment = fb.get("comment") or fb.get("note") or "No user note attached."
                    request_id = fb.get("request_id", "unknown_request")

                    clean_text = f"""# Helicone User Feedback Trace: {fb_id}
Target Request ID: {request_id}
Rating: {rating}

## User Comment / Feedback Note
{comment}
""".strip()
                    feedbacks.append({
                        "source_id": f"helicone:feedback:{fb_id}",
                        "feedback_id": fb_id,
                        "rating": rating,
                        "request_id": request_id,
                        "comment": comment,
                        "clean_text": clean_text,
                    })
                return feedbacks
        except Exception:
            return []

    async def fetch_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch all configured Helicone prompts, datasets, evaluators, and feedback traces."""
        async with aiohttp.ClientSession() as session:
            p_task = self.fetch_prompts(session)
            d_task = self.fetch_datasets(session)
            e_task = self.fetch_evaluators(session)
            f_task = self.fetch_feedback_traces(session)

            prompts, datasets, evaluators, feedbacks = await asyncio.gather(
                p_task, d_task, e_task, f_task
            )

            return {
                "prompts": prompts,
                "datasets": datasets,
                "evaluators": evaluators,
                "feedbacks": feedbacks,
            }
