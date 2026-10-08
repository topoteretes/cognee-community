# cognee-community-connector-salesforce

A Salesforce data-source connector for [cognee](https://github.com/topoteretes/cognee): turn CRM accounts, opportunities, support cases, and Chatter conversations into memory.

Syncs standard Salesforce CRM objects incrementally with **forget-on-delete** replication reconciliation.

---

## Features

- **OAuth 2.0 & Connected App Authentication**: Supports Web Server Flow (refresh token) and Username-Password + Security Token (for Developer / Sandbox orgs).
- **Automatic 401 Token Refresh**: Transparently catches expired access tokens, obtains a new token via the refresh endpoint, and retries requests.
- **Structured Relational Ingestion**: Unlike raw document connectors, Salesforce entities are ingested as structured relational tables (`DOCUMENT_SOURCE_ATTR = None`), preserving field types, schema attributes, and lookup relationships.
- **Incremental High-Watermark Sync**: Leverages SOQL queries filtered on `LastModifiedDate` with `nextRecordsUrl` pagination so only changed/new records are fetched after the initial backfill.
- **Replication-Based Forget-on-Delete**: Calls the Salesforce REST replication endpoint (`/sobjects/<Object>/deleted/`) to detect deleted records and emits tombstones (`{"_deleted": True}`) that drive Cognee's `orphan_cleanup`.
- **Global ID Namespacing**: Stable global IDs (`salesforce:account:{Id}`, `salesforce:opportunity:{Id}`, `salesforce:case:{Id}`, `salesforce:feeditem:{Id}`).

---

## Installation

```bash
uv pip install cognee-community-connector-salesforce
# or from this monorepo:
cd packages/connector/salesforce && uv sync --all-extras
```

---

## Setting Up a Salesforce Connected App

To connect via OAuth 2.0:

1. **Log in to Salesforce** (Developer Edition org or Sandbox).
2. Go to **Setup** $\rightarrow$ Search for **App Manager** $\rightarrow$ Click **New Connected App**.
3. Fill in basic details:
   - **Connected App Name**: `Cognee Connector`
   - **Contact Email**: your email address
4. Under **API (Enable OAuth Settings)**:
   - Check **Enable OAuth Settings**.
   - **Callback URL**: `http://localhost:8080/callback` (or your application redirect URI).
   - In **Selected OAuth Scopes**, add:
     - `Manage user data via APIs (api)`
     - `Perform requests at any time (refresh_token, offline_access)`
5. Click **Save** and wait 2–10 minutes for changes to propagate.
6. Click **Manage Consumer Details** to view and copy your **Consumer Key** (`client_id`) and **Consumer Secret** (`client_secret`).
7. Obtain your initial `refresh_token` by completing the authorization code flow, or use username/password + security token in a Developer Edition org.

---

## Usage

```python
import asyncio
import os
import cognee
from cognee_community_connector_salesforce import salesforce_source

async def main():
    source = salesforce_source(
        instance_url=os.environ["SALESFORCE_INSTANCE_URL"],
        client_id=os.environ["SALESFORCE_CLIENT_ID"],
        client_secret=os.environ["SALESFORCE_CLIENT_SECRET"],
        refresh_token=os.environ["SALESFORCE_REFRESH_TOKEN"],
        # Or username / password + security_token for developer orgs
        objects=["Account", "Opportunity", "Case", "FeedItem"],
    )

    # Ingest into Cognee memory
    await cognee.remember(
        source,
        dataset_name="salesforce_crm",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,  # disable row limit so orphan cleanup sees entire corpus
    )

    # Query CRM memory
    answer = await cognee.search(
        query_text="What are our top active opportunities and their current stages?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["salesforce_crm"],
    )
    print(answer)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Supported Objects & SOQL Fields

| Object | Global ID Prefix | Key Fields Synced | Foreign Key Links |
| :--- | :--- | :--- | :--- |
| **`Account`** | `salesforce:account:{Id}` | `Name`, `Industry`, `AnnualRevenue`, `BillingCity`, `Description`, `LastModifiedDate` | Owner |
| **`Opportunity`** | `salesforce:opportunity:{Id}` | `Name`, `Amount`, `StageName`, `Probability`, `CloseDate`, `Type`, `LastModifiedDate` | `account_id` $\rightarrow$ `salesforce:account:{AccountId}` |
| **`Case`** | `salesforce:case:{Id}` | `CaseNumber`, `Subject`, `Description`, `Status`, `Priority`, `Origin`, `LastModifiedDate` | `account_id`, `contact_id` |
| **`FeedItem`** | `salesforce:feeditem:{Id}` | `Title`, `Body`, `Type`, `ParentId`, `CreatedDate`, `LastModifiedDate` | `parent_id` |
| **`FeedComment`** | `salesforce:feedcomment:{Id}` | `CommentBody`, `ParentId`, `FeedItemId`, `CreatedDate`, `LastModifiedDate` | `feed_item_id` |

---

## Synchronization & Forget-on-Delete Architecture

1. **Initial Backfill**: The connector performs an initial SOQL scan of all requested objects ordered by `LastModifiedDate ASC` and records the highest timestamp seen in DLT resource state (`<Object>_last_sync`).
2. **Incremental Delta**: On subsequent runs, queries only fetch records where `LastModifiedDate >= :last_sync`.
3. **Replication `getDeleted`**: Concurrently, the connector queries Salesforce's `/services/data/v60.0/sobjects/<Object>/deleted/` endpoint. Any IDs deleted in Salesforce emit a tombstone row (`{"id": "...", "_deleted": True}`).
4. **Orphan Cleanup**: On `write_disposition="merge"`, DLT removes the marked rows, and Cognee's `orphan_cleanup` purges corresponding entities and relationships from the graph, vector, and relational stores.

---

## Running Offline Tests

The test suite runs 100% offline without network connections or Salesforce credentials:

```bash
python -m pytest tests/ -v
```
