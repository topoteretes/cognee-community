# Cognee TopK Adapter

## Installation

If published, the package can be simply installed via pip:

```bash
pip install cognee-community-vector-adapter-topk
```

In case it is not published yet, you can install it locally:

```bash
pip install -e . # run this command in the directory containing the pyproject.toml file
```

## Connection Setup

1. Create an account at [topk.io](https://www.topk.io)
2. Create a project and an API key in the [console](https://console.topk.io)
3. Set the `TOPK_API_KEY` environment variable
4. Optionally set `TOPK_REGION` (defaults to `aws-us-east-1-elastica`). See [available regions](https://docs.topk.io/regions).
5. For non-default deployments only, `TOPK_HOST` and `TOPK_HTTPS` override the API host (default `topk.io`) and TLS (`false` to disable).

## Usage

Import and register the adapter in your code:
```python
from cognee_community_vector_adapter_topk import register
```

Also, specify the dataset handler in the .env file:
```dotenv
VECTOR_DATASET_DATABASE_HANDLER="topk"
```

## Example
See example in `example.py` file.
