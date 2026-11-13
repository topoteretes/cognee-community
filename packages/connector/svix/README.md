# cognee-community-connector-svix

Svix webhooks and event infrastructure data-source connector for [Cognee](https://github.com/topoteretes/cognee).

Syncs applications, endpoint subscriptions, and event type schemas into memory so AI agents and site reliability engineers can reason over webhook architectures, debug delivery failures, and query event topology in natural language.

## Features

- **Full Resource Ingestion:**
  - **Applications:** App ID, name, UID, created timestamp, rate limits.
  - **Endpoints:** Target URL, description, subscribed event filter channels, disabled status, version.
  - **Event Types:** Name, description, schema definitions, archived status.
- **Strict Secret Redaction:** Webhook signing secrets (`whsec_...`) and custom authorization header values are completely redacted before reaching graph memory.
- **Incremental Synchronization:** SHA-256 canonical row fingerprinting to skip unmodified records and save embedding tokens.
- **Forget-on-Delete:** Emits `{"_deleted": True}` tombstones when applications, endpoints, or event types are deleted or archived upstream.
- **Resilient HTTP Client:** Cursor-based pagination (`iterator`), exponential backoff on HTTP 429/5xx errors.
- **100% Offline Test Suite:** Comprehensive unit and integration tests with zero live credential or external network requirements.

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_svix import svix, SVIX_REMEMBER_KWARGS


async def main():
    api_token = os.getenv("SVIX_API_TOKEN", "test_svix_token")
    source = svix(api_token=api_token)

    await cognee.remember(source, dataset_name="webhook_infra", **SVIX_REMEMBER_KWARGS)
    await cognee.cognify(dataset_name="webhook_infra")

    results = await cognee.search(
        search_text="Which endpoints are subscribed to user.signup?",
        dataset_name="webhook_infra",
    )
    for r in results:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
```
