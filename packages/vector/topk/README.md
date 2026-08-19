# Cognee TopK Adapter

## Installation

If published, the package can be simply installed via pip:

```bash
pip install cognee-community-vector-adapter-topk
```

## Setup

1. Create a TopK account [console.topk.io](https://console.topk.io).
2. Create a project or select the default project.
3. Create an API Key for your project (Project -> API Keys -> Create API Key).
4. Set the `TOPK_API_KEY` environment variable.
5. (Optional) Set `TOPK_REGION` environment variable to specify a region (defaults to `aws-us-east-1-elastica`). See [available regions](https://docs.topk.io/regions).

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

See `[example.py](./example.py)`