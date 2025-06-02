<div align="center">
  <a href="https://github.com/topoteretes/cognee">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee/refs/heads/dev/assets/cognee-logo-transparent.png" alt="Cognee Logo" height="60">
  </a>

  <br />

  cognee community - Memory for AI Agents in 5 lines of code

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

cognee builds AI memory, next generation tooling that is more accurate than RAG


## Get started

Install the package via command

```bash
uv pip install cognee-community-vector-adapter-qdrant
```
or any other adapter
```bash
uv pip install cognee-community-vector-adapter-azure
```
### with UV locally with all optional dependencies
Navigate to the packages folder and the adapter of choice

```bash
uv sync --all-extras
```

```
import os
os.environ["LLM_API_KEY"] = "YOUR OPENAI_API_KEY"

```
You can also set the variables by creating .env file, using our <a href="https://github.com/topoteretes/cognee/blob/main/.env.template">template.</a>
To use different LLM providers, for more info check out our <a href="https://docs.cognee.ai">documentation</a>


Navigate to the vector or graph store provider of choice and run the example

## Repository Structure

- **All packages are located in the `packages` directory.**
- **Each package must include:**
  - A `README.md` file with installation and usage instructions.
  - An `example.py` file demonstrating how to use the plugin with Cognee.


Contributions are welcome! Please ensure your package is well-documented and easy to use.
