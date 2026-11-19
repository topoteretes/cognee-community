import aiohttp
import asyncio
from typing import List, Dict, Any, Optional

class GrowthBookConnector:
    """
    GrowthBook Feature Flags & A/B Experimentation Connector for Cognee.
    
    Ingests feature flags, A/B experiments (hypotheses, variations, outcomes),
    and metric definitions from GrowthBook REST API v1 into Cognee memory graphs.
    """

    DEFAULT_BASE_URL = "https://api.growthbook.io"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        fetch_features_flag: bool = True,
        fetch_experiments_flag: bool = True,
        fetch_metrics_flag: bool = True,
        limit: int = 50,
    ):
        """
        Initialize GrowthBookConnector.

        Args:
            api_key: GrowthBook Secret API Key or Personal Access Token.
            base_url: Base URL (default: https://api.growthbook.io or custom self-hosted host).
            fetch_features_flag: Ingest feature flags and environment rules.
            fetch_experiments_flag: Ingest A/B experiments, hypotheses, and conclusions.
            fetch_metrics_flag: Ingest metric definitions.
            limit: Maximum items per endpoint (default: 50).
        """
        if not api_key:
            raise ValueError("GrowthBook API key must be provided.")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.fetch_features_flag = fetch_features_flag
        self.fetch_experiments_flag = fetch_experiments_flag
        self.fetch_metrics_flag = fetch_metrics_flag
        self.limit = max(1, min(limit, 200))

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-growthbook/0.1.0",
        }

    async def fetch_features(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch feature flags and their targeting/environment rules."""
        if not self.fetch_features_flag:
            return []

        url = f"{self.base_url}/api/v1/features"
        params = {"limit": str(self.limit)}
        try:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("features", data if isinstance(data, list) else [])
                features = []
                for f in items:
                    f_id = f.get("id", "unnamed_feature")
                    desc = f.get("description", "No description provided.")
                    default_val = f.get("defaultValue", "None")
                    value_type = f.get("valueType", "string")
                    tags = ", ".join(f.get("tags", [])) or "None"
                    date_updated = f.get("dateUpdated", "unknown")

                    clean_text = f"""# GrowthBook Feature Flag: {f_id}
Value Type: {value_type}
Default Value: {default_val}
Tags: {tags}
Last Updated: {date_updated}

## Description & Rules
{desc}
""".strip()
                    features.append({
                        "source_id": f"growthbook:feature:{f_id}",
                        "feature_id": f_id,
                        "value_type": value_type,
                        "default_value": default_val,
                        "date_updated": date_updated,
                        "data": f,
                        "clean_text": clean_text,
                    })
                return features
        except Exception:
            return []

    async def fetch_experiments(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch A/B experiments, hypotheses, variations, and conclusion outcomes."""
        if not self.fetch_experiments_flag:
            return []

        url = f"{self.base_url}/api/v1/experiments"
        params = {"limit": str(self.limit)}
        try:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("experiments", data if isinstance(data, list) else [])
                experiments = []
                for exp in items:
                    exp_id = exp.get("id", "unnamed_exp")
                    name = exp.get("name", exp_id)
                    status = exp.get("status", "draft")
                    hypothesis = exp.get("hypothesis", "No explicit hypothesis documented.")
                    desc = exp.get("description", "")
                    
                    # Parse variations
                    variations = exp.get("variations", [])
                    var_lines = []
                    for v in variations:
                        v_name = v.get("name", "Variation")
                        v_desc = v.get("description", "")
                        var_lines.append(f"- **{v_name}**: {v_desc}" if v_desc else f"- **{v_name}**")

                    # Parse conclusion / results
                    results_summary = exp.get("resultSummary", {})
                    conclusion = results_summary.get("conclusion") or exp.get("conclusion", "Ongoing / Undecided")
                    winner = results_summary.get("winner") or "None"

                    clean_text = f"""# GrowthBook Experiment: {name} (ID: {exp_id})
Status: {status}
Winning Variation: {winner}
Conclusion: {conclusion}

## Hypothesis
{hypothesis}

## Variations
{chr(10).join(var_lines) if var_lines else "Standard Control & Treatment"}

## Details
{desc if desc else 'Production A/B experiment.'}
""".strip()
                    experiments.append({
                        "source_id": f"growthbook:experiment:{exp_id}",
                        "experiment_id": exp_id,
                        "name": name,
                        "status": status,
                        "hypothesis": hypothesis,
                        "winner": winner,
                        "conclusion": conclusion,
                        "data": exp,
                        "clean_text": clean_text,
                    })
                return experiments
        except Exception:
            return []

    async def fetch_metrics(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Fetch metric definitions and evaluation criteria."""
        if not self.fetch_metrics_flag:
            return []

        url = f"{self.base_url}/api/v1/metrics"
        params = {"limit": str(self.limit)}
        try:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                items = data.get("metrics", data if isinstance(data, list) else [])
                metrics = []
                for m in items:
                    m_id = m.get("id", "unnamed_metric")
                    name = m.get("name", m_id)
                    m_type = m.get("type", "binomial")
                    desc = m.get("description", "Evaluation metric.")
                    clean_text = f"""# GrowthBook Metric: {name} (ID: {m_id})
Metric Type: {m_type}

## Description & Purpose
{desc}
""".strip()
                    metrics.append({
                        "source_id": f"growthbook:metric:{m_id}",
                        "metric_id": m_id,
                        "name": name,
                        "type": m_type,
                        "data": m,
                        "clean_text": clean_text,
                    })
                return metrics
        except Exception:
            return []

    async def fetch_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch all configured GrowthBook features, experiments, and metrics."""
        async with aiohttp.ClientSession() as session:
            f_task = self.fetch_features(session)
            e_task = self.fetch_experiments(session)
            m_task = self.fetch_metrics(session)

            features, experiments, metrics = await asyncio.gather(
                f_task, e_task, m_task
            )

            return {
                "features": features,
                "experiments": experiments,
                "metrics": metrics,
            }
