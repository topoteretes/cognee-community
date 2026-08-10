<div align="center">
  <a href="https://github.com/topoteretes/cognee">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee/refs/heads/dev/assets/cognee-logo-transparent.png" alt="Cognee Logo" height="60">
  </a>

  <br />

  cognee community - Memory for AI Agents in 6 lines of code

  <p align="center">
  <a href="https://www.youtube.com/watch?v=1bezuvLwJmw&t=2s">Demo</a>
  .
  <a href="https://cognee.ai">Learn more</a>
  ·
  <a href="https://discord.gg/NQPKmU5CCg">Join Discord</a>
  </p>


  [![GitHub forks](https://img.shields.io/github/forks/topoteretes/cognee.svg?style=social&label=Fork&maxAge=2592000)](https://GitHub.com/topoteretes/cognee/network/)
  [![GitHub stars](https://img.shields.io/github/stars/topoteretes/cognee.svg?style=social&label=Star&maxAge=2592000)](https://GitHub.com/topoteretes/cognee/stargazers/)
  [![GitHub commits](https://badgen.net/github/commits/topoteretes/cognee)](https://GitHub.com/topoteretes/cognee/commit/)
  [![Github tag](https://badgen.net/github/tag/topoteretes/cognee)](https://github.com/topoteretes/cognee/tags/)
  [![Downloads](https://static.pepy.tech/badge/cognee)](https://pepy.tech/project/cognee)
  [![License](https://img.shields.io/github/license/topoteretes/cognee?colorA=00C586&colorB=000000)](https://github.com/topoteretes/cognee/blob/main/LICENSE)
  [![Contributors](https://img.shields.io/github/contributors/topoteretes/cognee?colorA=00C586&colorB=000000)](https://github.com/topoteretes/cognee/graphs/contributors)



Welcome! This repository hosts community-managed plugins and addons for Cognee.

cognee builds AI memory, next-generation tooling that is more accurate than RAG
</div>




## Get started

This is a community-maintained plugins repo, where you can find various implementations of adapters, custom pipelines, etc.
You can check out our [core repo](https://github.com/topoteretes/cognee) on how to get started with cognee.

You can install the chosen community package in two different ways:

### Install using pip

Install the chosen community packaging using `uv pip install ...`

```bash
uv pip install cognee-community-vector-adapter-qdrant
```

### Install using UV or poetry locally with all optional dependencies

Navigate to the packages folder and the adapter of your choice, and run either of the following commands:

```bash
uv sync --all-extras
# OR
poetry install
```
OR
```bash
poetry install
```

You will need an LLM API key (OpenAI by default) in order to run cognee with these adapters.
Before importing cognee, make sure to define your key like so:
```
import os
os.environ["LLM_API_KEY"] = "YOUR OPENAI_API_KEY"
```
You can also set the variables by creating a `.env` file, using our <a href="https://github.com/topoteretes/cognee/blob/main/.env.template">template.</a>
To use different LLM providers, for more info check out our <a href="https://docs.cognee.ai">documentation</a>.

### Run an example to verify installation

Navigate to the package directory of your choice and run the example, usually found in the **examples** directory.
You can run them either via uv, or poetry:

```bash
uv run python ./examples/example.py
# OR
poetry run python ./examples/example.py
```

## Supported Database Adapters

| Package Name                                 | Type     | Description                                        |
|----------------------------------------------|----------|----------------------------------------------------|
| `cognee-community-vector-adapter-azure`      | Vector   | Azure AI search vector database adapter for cognee |
| `cognee-community-vector-adapter-milvus`     | Vector   | Milvus vector database adapter for cognee          |
| `cognee-community-vector-adapter-opensearch` | Vector   | Opensearch vector database adapter for cognee      |
| `cognee-community-vector-adapter-pinecone`   | Vector   | Pinecone vector database adapter for cognee        |
| `cognee-community-vector-adapter-qdrant`     | Vector   | Qdrant vector database adapter for cognee          |
| `cognee-community-vector-adapter-redis`      | Vector   | Redis vector database adapter for cognee           |
| `cognee-community-vector-adapter-topk`       | Vector   | TopK vector database adapter for cognee            |
| `cognee-community-vector-adapter-valkey`     | Vector   | Valkey vector database adapter for cognee          |
| `cognee-community-vector-adapter-weaviate`   | Vector   | Weaviate vector database adapter for cognee        |
| `cognee-community-graph-adapter-memgraph`    | Graph    | Memgraph graph database adapter for cognee         |
| `cognee-community-graph-adapter-networkx`    | Graph    | Networkx graph database adapter for cognee         |
| `cognee-community-graph-adapter-spanner`    | Graph    | Google Cloud Spanner (Spanner Graph) adapter for cognee |
| `cognee-community-graph-adapter-turingdb`   | Graph    | TuringDB graph database adapter for cognee         |
| `cognee-community-graph-adapter-pggraph`    | Graph    | pgGraph adapter for cognee (Postgres + graph extension, experimental) |
| `cognee-community-hybrid-adapter-duckdb`     | Hybrid   | DuckDB hybrid database adapter for cognee          |
| `cognee-community-hybrid-adapter-falkor`     | Hybrid   | FalkorDB hybrid database adapter for cognee        |

## Custom Packages

Custom packages are also a part of this repo, containing, for example, custom pipelines, tasks, and retrievers.
Every pipeline has its own package, as well as every retriever. Tasks are grouped so that all mutually relevant tasks
are contained in one package (i.e. all tasks used in one pipeline are packaged together).

### Current Custom Packages

| Package Name                       | Type       | Description                    |
|------------------------------------|------------|--------------------------------|
| `cognee-community-pipeline-codify`     | Pipeline   | Custom codify pipeline package                                        |
| `cognee-community-retriever-code`      | Retriever  | Custom CODE retriever package                                         |
| `cognee-community-tasks-codify`        | Task       | Custom codify tasks package                                           |
| `cognee-community-tasks-scrapegraph`   | Task       | Web scraping tasks powered by [ScrapeGraphAI](https://github.com/ScrapeGraphAI/scrapegraph-py) |
| `cognee-community-tasks-exa`           | Task       | Web search tasks powered by [Exa](https://exa.ai)                     |

## Repository Structure

- **All packages are located in the `packages` directory.**
- **Each package must include:**
  - A `README.md` file with installation and usage instructions.
  - An `examples` directory containing an `example.py` file demonstrating how to use the plugin with Cognee.
  - A `tests` directory containing tests at least a bit more detailed than the example.
- **When adding new adapters or custom packages, follow the structure of the existing packages.**

## Contributing
We welcome contributions from the community! Your input helps make Cognee better for everyone. See [`CONTRIBUTING.md`](CONTRIBUTING.md) to get started.
