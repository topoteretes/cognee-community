# Cognee Community Code Retriever

`cognee-community-retriever-code` adds the `CODE` search type to Cognee. It uses the configured LLM to extract likely filenames and source code from a query, searches the code filename and code-part collections, then returns graph connections for the matching records.

The retriever expects a code graph to have already been created. The companion [Codify pipeline](https://github.com/topoteretes/cognee-community/tree/main/packages/pipeline/codify_pipeline) is one supported way to build those collections.

## Install

Install the retriever and the Codify pipeline in the same environment:

```bash
python -m pip install cognee-community-retriever-code cognee-community-pipeline-codify
```

Configure Cognee's LLM and graph/vector database providers using the [Cognee configuration guide](https://docs.cognee.ai/). The example uses those configured services; it does not include provider credentials.

## Run the example

Choose a small repository whose source you are permitted to send to your configured providers, then set its path and run the example:

```bash
export REPOSITORY_PATH=/path/to/a/small/repository
python examples/example.py
```

The example runs the Codify pipeline and then performs a `CODE` search. It does not prune or clear an existing Cognee dataset. Configure an isolated Cognee data environment if you want to keep the example's records separate from other data.

## Use the search type

After the code graph has been built, register the retriever before searching:

```python
import cognee
from cognee import SearchType
from cognee_community_retriever_code import register  # noqa: F401
from cognee_community_retriever_code.code_retriever import CodeSearchType

results = await cognee.search(
    query_type=SearchType[CodeSearchType.name],
    query_text="Find the function that validates repository paths",
)
```

## Behavior and limits

- The query is interpreted by the configured LLM; results can vary by provider and model.
- Retrieval requires the expected code filename and code-part collections to exist.
- Results are graph connections returned by the configured Cognee graph engine. This package does not independently verify that a natural-language claim is supported by a particular line of source code.
- An empty or whitespace-only query raises `ValueError` before database or model access.

## Tests

From this package directory, run:

```bash
uv run --with pytest pytest
```
