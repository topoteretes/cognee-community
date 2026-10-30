"""Persist Fireflies speaker identities independently of LLM extraction."""

import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

_SYNC_METADATA_KEY = "fireflies_speaker_sync"


def speaker_node_id(dataset_id, transcript_id: str, speaker_key: str):
    # Fireflies speaker IDs identify a speaker within a transcript, not a
    # globally verified person. Never merge people solely by name or ID.
    identity = json.dumps([str(dataset_id), transcript_id, speaker_key], ensure_ascii=False)
    return uuid5(NAMESPACE_URL, "cognee:fireflies:speaker:" + identity)


async def preserve_speaker_sync_metadata(documents, dataset):
    """Cognee add replaces external_metadata; carry successful checkpoints forward."""
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data

    existing = {item.id: item for item in await get_dataset_data(dataset.id)}
    for document in documents:
        previous = existing.get(document.data_id)
        checkpoints = (
            (previous.external_metadata or {}).get(_SYNC_METADATA_KEY) if previous else None
        )
        if checkpoints:
            document.external_metadata[_SYNC_METADATA_KEY] = checkpoints


async def store_speaker_entities(documents, speaker_records, *, dataset, user):
    """Write Person entities and spoke_in edges with normal Cognee provenance.

    Checkpoints are committed only after graph and vector writes both succeed.
    A failed write can be retried on an otherwise unchanged source. Existing
    installations without checkpoints receive speaker nodes on their next sync.
    """
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.relational import get_async_session
    from cognee.modules.data.models import Data
    from cognee.modules.engine.models import Entity, EntityType
    from cognee.modules.pipelines.models import PipelineContext
    from cognee.tasks.storage import add_data_points
    from sqlalchemy import update

    async with set_database_global_context_variables(dataset.id, user.id):
        graph = await get_graph_engine()
        person_type = EntityType(name="Person", description="A person speaking in a meeting.")
        for document in documents:
            metadata = document.external_metadata
            transcript_id = metadata["external_id"]
            speakers = speaker_records[transcript_id]
            if not speakers:
                continue
            fingerprint = hashlib.sha256(
                json.dumps([1, speakers], sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            checkpoints = dict(metadata.get(_SYNC_METADATA_KEY) or {})
            if checkpoints.get(str(dataset.id)) == fingerprint:
                continue
            if await graph.get_node(str(document.data_id)) is None:
                raise RuntimeError(
                    "Fireflies document graph is missing; finish cognify before speakers."
                )

            entities, edges = [], []
            for speaker in speakers:
                meeting_title = metadata.get("title") or transcript_id
                entity = Entity(
                    id=speaker_node_id(dataset.id, transcript_id, speaker["key"]),
                    name=speaker["name"],
                    description=(
                        f"Fireflies speaker {speaker['speaker_id'] or speaker['name']} "
                        f"in meeting {transcript_id}."
                    ),
                    is_a=person_type,
                )
                entities.append(entity)
                edges.append(
                    (
                        str(entity.id),
                        str(document.data_id),
                        "spoke_in",
                        {
                            "source_node_id": str(entity.id),
                            "target_node_id": str(document.data_id),
                            "relationship_name": "spoke_in",
                            "edge_text": f"{speaker['name']} spoke in {meeting_title}.",
                            "fireflies_speaker_id": speaker["speaker_id"],
                            "fireflies_transcript_id": transcript_id,
                        },
                    )
                )
            await add_data_points(
                entities,
                custom_edges=edges,
                ctx=PipelineContext(user=user, dataset=dataset, data_item=document),
            )
            checkpoints[str(dataset.id)] = fingerprint
            updated_metadata = {**metadata, _SYNC_METADATA_KEY: checkpoints}
            async with get_async_session() as session:
                await session.execute(
                    update(Data)
                    .where(Data.id == document.data_id)
                    .values(external_metadata=updated_metadata)
                )
                await session.commit()
            document.external_metadata = updated_metadata
