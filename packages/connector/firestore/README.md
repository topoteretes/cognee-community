
# Cognee Community Firestore Connector

A Google Cloud Firestore connector for ingesting documents from a selected Firestore collection into Cognee.

## Features

- Connects to Google Cloud Firestore using Application Default Credentials.
- Supports selecting a Firestore collection.
- Converts Firestore documents into Cognee-compatible document rows.
- Tracks document changes between syncs.
- Emits deletion markers for documents removed from the collection.
- Supports an optional timestamp field for change detection.
- Allows a Firestore client to be injected for testing.

## Requirements

- Python 3.11–3.13
- A Google Cloud project with Firestore enabled.
- Appropriate Firestore read permissions.
- Google Cloud Application Default Credentials configured.

## Installation

From the root of the `cognee-community` repository, run:

```bash
python -m pip install -e "./packages/connector/firestore[dev]"
```

## Authentication

Configure Google Cloud Application Default Credentials before running the connector.

For local development, you can authenticate using the Google Cloud CLI:

```bash
gcloud auth application-default login
```

Alternatively, set the path to a service-account JSON key:

**PowerShell:**

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\service-account.json"
$env:GOOGLE_CLOUD_PROJECT = "your-google-cloud-project-id"
```

Use a service account with only the permissions required to read the intended Firestore data. Never commit credentials or service-account keys to GitHub.

## Usage

```python
from cognee_community_connector_firestore import firestore_source

source = firestore_source(
    collection="customers",
    project_id="your-google-cloud-project-id",
)

# Pass the source to the Cognee/dlt ingestion workflow
# used by your application.
```

To use a named timestamp field for change detection:

```python
source = firestore_source(
    collection="customers",
    project_id="your-google-cloud-project-id",
    timestamp_field="updated_at",
)
```

When using `timestamp_field`, each document must contain that field. Update it whenever the document changes.

## Testing

Install the development dependencies and run the tests from the repository root:

```bash
python -m pip install -e "./packages/connector/firestore[dev]"
python -m pytest packages/connector/firestore/tests -v
```

The unit tests use mock data and do not require access to a live Firestore project.

## Limitations

- The connector currently scans the selected collection to detect changes and deletions.
- Firestore document references are represented as paths in the serialized document data; they are not yet converted into graph relationships.
- A real Firestore project is required to validate authentication and end-to-end ingestion.

## Security

Do not commit service-account credentials, access tokens, or other secrets. Follow your organization's Google Cloud security policies.
