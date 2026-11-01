"""Offline-of-LLM smoke check: run the Deel connector against a throwaway sqlite database.

No LLM and no cognee memory are involved, so it costs nothing. It prints only counts and the
per-resource run summary (never record content). Run 2 re-sends only records that changed or
sit inside the ``updated_at`` overlap window; run 3 uses no overlap to show the difference.
A final step simulates an upstream deletion (read-only) by narrowing the contracts filter.

    export DEEL_API_TOKEN=...   DEEL_BASE_URL=https://api-staging.letsdeel.com/rest
    uv run python examples/check_sandbox.py
"""

import tempfile

import dlt

from cognee_community_connector_deel import deel_source


def hashes(pipeline, table):
    known = pipeline.state["sources"]["deel"]["resources"][table]["known"]
    return {record_id: entry.split(":")[0] for record_id, entry in known.items()}


def count(pipeline, table):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT count(*) FROM {table}") as cursor,
    ):
        return cursor.fetchone()[0]


def simulate_upstream_deletion(pipeline):
    """Read-only deletion test: narrow the server-side filter so some contracts "vanish".

    From the connector's point of view a contract that stops being returned is a contract
    deleted upstream, so the next pass must tombstone it and dlt must drop its row.
    """
    for column, option in (("contract_type", "contract_types"), ("status", "contract_statuses")):
        with (
            pipeline.sql_client() as client,
            client.execute_query(
                f"SELECT {column}, count(*) FROM deel_contracts GROUP BY 1 ORDER BY 2 DESC"
            ) as cursor,
        ):
            groups = cursor.fetchall()
        if len(groups) > 1:
            keep, keep_count = groups[0]
            chosen = option
            break
    else:
        print("RESULT deletion test skipped: all contracts share one type and one status")
        return
    before = count(pipeline, "deel_contracts")
    pipeline.run(
        deel_source(resources=["contracts"], force_delete=True, **{chosen: [keep]}),
        write_disposition="merge",
        primary_key="id",
    )
    state = pipeline.state["sources"]["deel"]["resources"]["deel_contracts"]
    after = count(pipeline, "deel_contracts")
    print(
        f"RESULT deletion test (filter {chosen}=[{keep!r}]): rows {before} -> {after} "
        f"(expected {keep_count}), tombstoned={state['last_run']['tombstoned']} "
        f"(expected {before - keep_count}), sweep={state['last_run']['sweep']}"
    )


def main() -> None:
    workdir = tempfile.mkdtemp()
    pipeline = dlt.pipeline(
        "deel_check",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{workdir}/check.db"),
        dataset_name="deel_check",
        pipelines_dir=f"{workdir}/state",
    )
    previous = {}
    for run, overlap in ((1, 3600), (2, 3600), (3, 0)):
        pipeline.run(
            deel_source(overlap_seconds=overlap), write_disposition="merge", primary_key="id"
        )
        for table in ("deel_contracts", "deel_people"):
            state = pipeline.state["sources"]["deel"]["resources"][table]
            current = hashes(pipeline, table)
            changed = sum(1 for k, v in current.items() if previous.get(table, {}).get(k, v) != v)
            previous[table] = current
            print(
                f"RESULT run {run} (overlap {overlap}s): {table} "
                f"fetched={state['last_run']['fetched']} emitted={state['last_run']['emitted']} "
                f"hash_changed_vs_previous_run={changed} cursor={state.get('cursor')}"
            )
    simulate_upstream_deletion(pipeline)


if __name__ == "__main__":
    main()
