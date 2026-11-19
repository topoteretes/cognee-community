"""Braintrust AI evaluations, experiments, datasets, and prompt templates connector for Cognee.

Ingests AI evaluation experiments, model scores, evaluation datasets, and prompt engineering
templates from Braintrust into Cognee memory for automated LLMOps and RAG benchmarking intelligence.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Generator, List, Optional
import dlt
import httpx

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger(__name__)

BRAINTRUST_API_BASE_URL = "https://api.braintrust.dev/v1"


class BraintrustClient:
    """Client for querying the Braintrust REST API v1."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BRAINTRUST_API_BASE_URL,
        http_client: Optional[Any] = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "CogneeCommunityBraintrustConnector/0.1.0",
            "Accept": "application/json",
        }
        self._http_client = http_client or httpx.Client(
            timeout=timeout,
            headers=headers,
        )

    def get_projects(self) -> List[Dict[str, Any]]:
        """Retrieve all accessible Braintrust projects."""
        url = f"{self.base_url}/projects"
        try:
            resp = self._http_client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get("objects", data) if isinstance(data, dict) else data
        except Exception as exc:
            logger.error("Failed to fetch Braintrust projects: %s", exc)
            raise

    def get_experiments(self, project_id: str) -> List[Dict[str, Any]]:
        """Retrieve evaluation experiments for a specific project."""
        url = f"{self.base_url}/experiments"
        params = {"project_id": project_id}
        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("objects", data) if isinstance(data, dict) else data
        except Exception as exc:
            logger.error("Failed to fetch Braintrust experiments for project %s: %s", project_id, exc)
            raise

    def get_datasets(self, project_id: str) -> List[Dict[str, Any]]:
        """Retrieve evaluation datasets for a specific project."""
        url = f"{self.base_url}/datasets"
        params = {"project_id": project_id}
        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("objects", data) if isinstance(data, dict) else data
        except Exception as exc:
            logger.error("Failed to fetch Braintrust datasets for project %s: %s", project_id, exc)
            raise

    def get_prompts(self, project_id: str) -> List[Dict[str, Any]]:
        """Retrieve registered prompt templates for a project."""
        url = f"{self.base_url}/prompts"
        params = {"project_id": project_id}
        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("objects", data) if isinstance(data, dict) else data
        except Exception as exc:
            logger.error("Failed to fetch Braintrust prompts for project %s: %s", project_id, exc)
            raise


def _project_to_document(project: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a Braintrust project into a structured Markdown document."""
    proj_id = project.get("id")
    name = project.get("name")
    if not proj_id or not name:
        return None

    description = project.get("description") or "No project description provided."
    created = project.get("created", "Unknown")
    url = f"https://www.braintrust.dev/app/{name}"

    content = f"""# Braintrust Project: {name}
**Project ID:** `{proj_id}`
**URL:** [{name}]({url})
**Created:** {created}
**Description:** {description}

---
*Ingested from Braintrust AI Evaluation Platform via Cognee community connector.*"""

    return {
        "id": f"bt_proj_{proj_id}",
        "url": url,
        "title": f"Braintrust Project: {name}",
        "content": content,
    }


def _experiment_to_document(exp: Dict[str, Any], project_name: str) -> Optional[Dict[str, Any]]:
    """Convert a Braintrust evaluation experiment into a structured Markdown document."""
    exp_id = exp.get("id")
    name = exp.get("name")
    if not exp_id or not name:
        return None

    description = exp.get("description") or "No experiment description recorded."
    dataset_id = exp.get("dataset_id") or "None"
    created = exp.get("created", "Unknown")
    metadata = exp.get("metadata", {})
    metrics = exp.get("scores", {})

    url = f"https://www.braintrust.dev/app/{project_name}/experiments/{name}"

    metrics_lines = []
    if metrics:
        for k, v in metrics.items():
            metrics_lines.append(f"- **{k}:** {v}")

    metadata_lines = []
    if metadata:
        for k, v in metadata.items():
            metadata_lines.append(f"- **{k}:** {v}")

    content = f"""# Braintrust Evaluation Experiment: {name}
**Project:** {project_name}
**Experiment ID:** `{exp_id}`
**Associated Dataset ID:** `{dataset_id}`
**Created:** {created}
**Description:** {description}
**Dashboard Link:** [{name}]({url})

## Evaluation Scores & Metrics:
{chr(10).join(metrics_lines) if metrics_lines else "- No benchmark scores recorded."}

## Model & Experiment Configuration:
{chr(10).join(metadata_lines) if metadata_lines else "- Default configuration."}

---
*Ingested from Braintrust Evaluation Platform via Cognee community connector.*"""

    return {
        "id": f"bt_exp_{exp_id}",
        "url": url,
        "title": f"Braintrust Experiment: {name} ({project_name})",
        "content": content,
    }


def _dataset_to_document(ds: Dict[str, Any], project_name: str) -> Optional[Dict[str, Any]]:
    """Convert a Braintrust eval dataset into a structured Markdown document."""
    ds_id = ds.get("id")
    name = ds.get("name")
    if not ds_id or not name:
        return None

    description = ds.get("description") or "No dataset description."
    created = ds.get("created", "Unknown")
    url = f"https://www.braintrust.dev/app/{project_name}/datasets/{name}"

    content = f"""# Braintrust Evaluation Dataset: {name}
**Project:** {project_name}
**Dataset ID:** `{ds_id}`
**Created:** {created}
**Description:** {description}
**Dashboard Link:** [{name}]({url})

---
*Ingested from Braintrust Evaluation Platform via Cognee community connector.*"""

    return {
        "id": f"bt_ds_{ds_id}",
        "url": url,
        "title": f"Braintrust Dataset: {name} ({project_name})",
        "content": content,
    }


def _prompt_to_document(prompt: Dict[str, Any], project_name: str) -> Optional[Dict[str, Any]]:
    """Convert a Braintrust prompt template into a structured Markdown document."""
    prompt_id = prompt.get("id")
    name = prompt.get("name")
    if not prompt_id or not name:
        return None

    description = prompt.get("description") or "No prompt description."
    prompt_data = prompt.get("prompt_data", {})
    model = prompt_data.get("options", {}).get("model", "Default Model")
    template = prompt_data.get("prompt", {}).get("messages", [])

    url = f"https://www.braintrust.dev/app/{project_name}/prompts/{name}"

    template_str = str(template) if template else "No explicit message template recorded."

    content = f"""# Braintrust Prompt Template: {name}
**Project:** {project_name}
**Prompt ID:** `{prompt_id}`
**Target Model:** `{model}`
**Description:** {description}
**Dashboard Link:** [{name}]({url})

## Template Body:
```json
{template_str}
```

---
*Ingested from Braintrust Prompt Registry via Cognee community connector.*"""

    return {
        "id": f"bt_prompt_{prompt_id}",
        "url": url,
        "title": f"Braintrust Prompt: {name} ({project_name})",
        "content": content,
    }


@dlt.source(name="braintrust_source")
def braintrust_source(
    api_key: Optional[str] = None,
    project_names: Optional[List[str]] = None,
    include_prompts: bool = True,
    include_datasets: bool = True,
    client: Optional[BraintrustClient] = None,
) -> Any:
    """DLT data-source for ingesting Braintrust AI evaluations into Cognee."""
    bt_client = client or BraintrustClient(api_key=api_key or "")

    @dlt.resource(
        name="braintrust_ai_evals",
        write_disposition="replace",
        primary_key="id",
    )
    def braintrust_ai_evals() -> Generator[Dict[str, Any], None, None]:
        all_projects = bt_client.get_projects()
        target_projects = [
            p for p in all_projects
            if not project_names or p.get("name") in project_names
        ]

        for proj in target_projects:
            proj_id = proj.get("id")
            proj_name = proj.get("name", "Project")
            proj_doc = _project_to_document(proj)
            if proj_doc:
                yield proj_doc

            if proj_id:
                # 1. Ingest evaluation experiments
                experiments = bt_client.get_experiments(project_id=proj_id)
                for exp in experiments:
                    exp_doc = _experiment_to_document(exp, project_name=proj_name)
                    if exp_doc:
                        yield exp_doc

                # 2. Ingest evaluation datasets
                if include_datasets:
                    datasets = bt_client.get_datasets(project_id=proj_id)
                    for ds in datasets:
                        ds_doc = _dataset_to_document(ds, project_name=proj_name)
                        if ds_doc:
                            yield ds_doc

                # 3. Ingest prompt templates
                if include_prompts:
                    prompts = bt_client.get_prompts(project_id=proj_id)
                    for pr in prompts:
                        pr_doc = _prompt_to_document(pr, project_name=proj_name)
                        if pr_doc:
                            yield pr_doc

    resource = braintrust_ai_evals()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "braintrust")
    return resource
