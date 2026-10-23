"""Example usage of cognee-community-connector-jupyter.

Ingests local Jupyter notebooks into Cognee and performs semantic GraphRAG search
over analysis notes, code, and execution outputs.
"""

import asyncio
from pathlib import Path

try:
    import cognee

    from cognee_community_connector_jupyter import jupyter_source
except ImportError as exc:
    raise SystemExit(
        "Missing requirements. Install with:\n"
        "  pip install cognee 'cognee-community-connector-jupyter'\n"
    ) from exc


async def main():
    # 1. Point to your data science notebooks directory
    notebooks_dir = Path("./notebooks")
    notebooks_dir.mkdir(exist_ok=True)

    sample_nb = notebooks_dir / "churn_eda.ipynb"
    if not sample_nb.exists():
        sample_nb.write_text(
            """{
  "nbformat": 4,
  "nbformat_minor": 2,
  "metadata": {"language_info": {"name": "python"}},
  "cells": [
    {
      "cell_type": "markdown",
      "source": "# Customer Churn Exploratory Analysis\\nTenure vs churn rates using Random Forest."
    },
    {
      "cell_type": "code",
      "source": "model = RandomForestClassifier(n_estimators=100)\\nmodel.fit(X_train, y_train)",
      "outputs": [{"output_type": "stream", "text": "Training accuracy: 94.2%\\n"}]
    }
  ]
}""",
            encoding="utf-8",
        )

    print("🚀 Initializing Jupyter connector...")
    source = jupyter_source(
        workspace_dir=notebooks_dir,
        include_outputs=True,
        redact_secrets=True,
    )

    # 2. Ingest into Cognee memory (document-mode + forget-on-delete)
    print("📥 Ingesting notebook documents into Cognee...")
    await cognee.add(source)
    await cognee.cognify()

    # 3. Search across notebook graph memory
    query = "What machine learning model was used for customer churn and what was its accuracy?"
    print(f"\n🔍 Querying Cognee: '{query}'")
    results = await cognee.search(query)

    print("\n💡 Results:")
    for res in results:
        print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
