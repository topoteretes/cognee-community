# cognee-community-connector-jupyter

Jupyter Notebook (`.ipynb`) data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests notebooks into Cognee memory, extracting narrative markdown cells and code cells with execution outputs into connected graph and vector memory.

## Usage

```python
import cognee
from cognee_community_connector_jupyter import jupyter_notebook_source

# Ingest a single notebook or an entire directory of notebooks
await cognee.remember(
    jupyter_notebook_source("./notebooks"),
    dataset_name="data-science-eda",
)
```

## Features

- **Prose & Code segregation**: Extracts markdown descriptions, code blocks, and execution results.
- **Output sanitization**: Captures stdout, stderr, and text representations while stripping heavy binary image payloads.
- **Hierarchical tracking**: Cells are tagged with their parent notebook name, relative index, and execution counts.
- **Recursive scanning**: Ingests individual notebooks or scans directories recursively.
