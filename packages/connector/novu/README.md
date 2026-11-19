# cognee-community-connector-novu

Novu notification infrastructure data-source connector for [Cognee](https://github.com/topoteretes/cognee).

Syncs notification workflows, topics, notification groups, and integration providers into memory so AI agents and operations teams can reason over multi-channel notification topologies, debug delivery paths, and query alert configurations in natural language.

## Features

- **Full Resource Coverage:**
  - **Workflows:** Name, description, active status, channel steps (email, SMS, push, in-app, chat), and tags.
  - **Topics:** Topic key, name, subscriber count.
  - **Notification Groups:** Category ID, group name.
  - **Integrations:** Delivery provider ID, channel type, active status.
- **Strict Credential Redaction:** Provider API keys and secret tokens are strictly redacted before reaching graph memory.
- **Incremental Synchronization:** SHA-256 canonical row hashing to skip unmodified entities.
- **Forget-on-Delete:** Emits `{"_deleted": True}` tombstones when workflows, topics, or integrations are deleted upstream.
- **Resilient HTTP Client:** Automatic pagination, exponential backoff on HTTP 429/5xx errors.
- **100% Offline Test Suite:** Comprehensive unit and integration tests with zero live credentials or external network access.

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_novu import novu, NOVU_REMEMBER_KWARGS


async def main():
    api_key = os.getenv("NOVU_API_KEY", "test_novu_api_key")
    source = novu(api_key=api_key)

    await cognee.remember(source, dataset_name="notification_infra", **NOVU_REMEMBER_KWARGS)
    await cognee.cognify(dataset_name="notification_infra")

    results = await cognee.search(
        search_text="Which notification workflows send SMS alerts?",
        dataset_name="notification_infra",
    )
    for r in results:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
```
