# cognee-community-connector-jenkins

A Jenkins data-source connector for [cognee](https://github.com/topoteretes/cognee). It syncs
selected Jenkins job configuration and build outcomes into cognee documents, including bounded
console output for failed builds.

## Install

```bash
cd packages/connector/jenkins
uv sync
```

The connector uses Jenkins' Remote Access API with HTTP Basic authentication. Use a Jenkins
username and that account's API token; do not use the account password. The account needs
read access to the selected jobs and their builds.

```bash
export JENKINS_URL="http://127.0.0.1:8080"
export JENKINS_USER="your-jenkins-user"
printf 'Jenkins API token: '
read -r -s JENKINS_API_TOKEN
printf '\n'
export JENKINS_API_TOKEN
export LLM_API_KEY="your-cognee-llm-key"
uv run python examples/example.py
```

Credentials can also be passed to `jenkins_source(...)`. Environment variables are convenient
for local development; keep them out of source control and shell history.

## Usage

```python
import cognee
from cognee_community_connector_jenkins import jenkins_source

await cognee.remember(
    jenkins_source(
        job_names=["team/service"],  # Jenkins fullName; omit to sync all visible jobs
        include_job_config=True,
        include_builds=True,
    ),
    dataset_name="jenkins",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Use `job_names` to select jobs (folder names are slash-separated). Set
`include_job_config=False` or `include_builds=False` to exclude either content type. The first
sync includes up to 20 recent builds per job; pass `initial_builds=N` to change that history
window. Later syncs fetch every newer build after each job's own `lastBuild` checkpoint and
revisit builds that were still running on the prior sync.

## What is ingested

Each job is a document with its name, description, Jenkins job type, disabled state, and
buildable state. Each completed build is a separate document with its outcome, timestamp,
duration, and Jenkins URL. Console text is requested only for `FAILURE` builds and is capped at
128 KiB per build. Jenkins API requests select only the required fields and explicitly set
`depth=1`.

The connector deliberately does not request raw `config.xml`. That endpoint can contain
credential bindings, parameter defaults, and arbitrary build scripts. The job document uses a
small allowlist of operational fields instead.

## Incremental sync and deletion

DLT stores a `lastBuild` watermark, pending in-progress build numbers, known jobs, and emitted
build numbers in per-resource state. Stable document IDs combine the Jenkins base URL, the job's
`fullName`, and (for builds) the build number. If a job disappears from a complete inventory,
the connector emits DLT hard-delete markers for the job and its ingested builds. DLT removes
those rows on `merge`; cognee's foreground `orphan_cleanup` then removes the associated memory.

An empty unscoped inventory is ambiguous: it can indicate lost visibility as well as deletion.
In this case the connector preserves the previous state and documents. With an explicit
`job_names` selection, an empty inventory is treated as a deletion of the selected jobs.
Run foreground `cognee.remember` calls and use a dedicated dataset for the Jenkins selection.

## Example and tests

See [`examples/example.py`](examples/example.py) for a runnable sync and search example.
Run the mocked and DLT-backed tests with:

```bash
uv run pytest tests/
```

Tests cover bounded API queries, nested folders, successful and failed builds, capped failure
logs, in-progress builds, per-job incremental state, job selection, and hard-delete behavior.
