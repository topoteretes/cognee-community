"""Foreground sync, including Cognee 1.4.0's empty-corpus cleanup edge case."""

from typing import Any

from .fireflies import _SPEAKER_RECORDS_ATTR, FIREFLIES_SOURCE_NAME
from .speakers import preserve_speaker_sync_metadata, store_speaker_entities


async def sync_fireflies(source: Any, *, dataset_name: str = "fireflies_meetings", user=None):
    """Load a Fireflies resource, reconcile memory and cognify its documents.

    Use one dataset per Fireflies account/scope and run syncs sequentially.
    The ordinary ``cognee.add``/``remember`` interface remains supported, but
    Cognee 1.4.0 skips orphan cleanup when the last staged document disappears.
    This helper explicitly handles that successful empty result. It does not
    treat a failed extraction/load/read as an empty corpus.
    """
    import cognee
    from cognee.api.v1.serve.state import get_remote_client
    from cognee.modules.engine.operations.setup import setup
    from cognee.modules.pipelines.layers.resolve_authorized_user_dataset import (
        resolve_authorized_user_dataset,
    )
    from cognee.tasks.ingestion.dlt_utils import document_source_tag
    from cognee.tasks.ingestion.resolve_dlt_sources import resolve_dlt_sources

    if get_remote_client() is not None:
        raise ValueError("sync_fireflies requires a local Cognee instance.")
    if document_source_tag(source) != FIREFLIES_SOURCE_NAME:
        raise ValueError("Pass a resource returned by fireflies_source().")
    speaker_records = getattr(source, _SPEAKER_RECORDS_ATTR, None)
    if speaker_records is None:
        raise ValueError("Pass a resource returned by fireflies_source().")

    await setup()
    user, dataset = await resolve_authorized_user_dataset(dataset_name=dataset_name, user=user)
    documents, cleanup = await resolve_dlt_sources(
        source,
        dataset_name=dataset_name,
        user=user,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    if documents:
        # The resolver has already materialized standard DataItems. Passing
        # them to add does not execute the source a second time.
        await preserve_speaker_sync_metadata(documents, dataset)
        await cognee.add(documents, dataset_name=dataset_name, user=user)
        if cleanup is not None:
            await cleanup()
        result = await cognee.cognify(datasets=[dataset_name], user=user)
        await store_speaker_entities(documents, speaker_records, dataset=dataset, user=user)
        return result

    # Empty means the complete source loaded and the full staging read-back
    # succeeded with no documents. Failures above propagate before deletion.
    from cognee.modules.data.methods.delete_data import delete_data
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.graph.methods.delete_data_nodes_and_edges import (
        delete_data_nodes_and_edges,
    )

    for item in await get_dataset_data(dataset.id):
        metadata = item.external_metadata
        if isinstance(metadata, dict) and metadata.get("source") == FIREFLIES_SOURCE_NAME:
            await delete_data_nodes_and_edges(dataset.id, item.id, user.id)
            await delete_data(item, dataset.id)
    return None
