# cognee-community-connector-unkey

Unkey API key infrastructure and rate-limiting data-source connector for [Cognee](https://github.com/topoteretes/cognee).

Syncs APIs, key metadata, identities, and rate limit rules into memory so AI agents and developer platforms can reason over API authentication topologies, inspect tenant quotas, and debug rate-limiting policies in natural language.

## Features

- **Full Resource Coverage:**
  - **APIs:** API ID, friendly name, workspace ID, description, creation timestamp.
  - **Key Metadata:** Key ID, name, owner ID, enabled status, remaining quota, refill interval, rate limits.
  - **Identities:** Identity ID, external ID, metadata tags.
- **Strict Key Redaction:** Plaintext API keys and cryptographic hashes are never emitted or indexed (`[Redacted & Protected]`).
- **Incremental Synchronization:** SHA-256 canonical row hashing skips unmodified entities.
- **Forget-on-Delete:** Emits `{"_deleted": True}` tombstones when APIs, keys, or identities are revoked or deleted upstream.
- **Resilient HTTP Client:** Cursor-based pagination, exponential backoff on HTTP 429/5xx errors.
- **100% Offline Test Suite:** Comprehensive unit and integration tests with zero external network access.

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_unkey import unkey, UNKEY_REMEMBER_KWARGS


async def main():
    root_key = os.getenv("UNKEY_ROOT_KEY", "unkey_test_token")
    source = unkey(root_key=root_key)

    await cognee.remember(source, dataset_name="unkey_infra", **UNKEY_REMEMBER_KWARGS)
    await cognee.cognify(dataset_name="unkey_infra")

    results = await cognee.search(
        search_text="Which API keys belong to tenant user_123 and are any disabled?",
        dataset_name="unkey_infra",
    )
    for r in results:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
```
