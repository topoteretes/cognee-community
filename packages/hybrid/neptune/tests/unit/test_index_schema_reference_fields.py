"""The Neptune IndexSchema must carry the reference scalars used by the search
"Evidence" feature.

Pure schema/serialization checks (no Neptune graph), guarding against the adapter
silently dropping ``document_id`` / ``document_name`` / ``chunk_index`` from its
payload, which would make chunk Evidence render empty on this backend.
"""

from cognee_community_hybrid_adapter_neptune.neptune_analytics_adapter import IndexSchema

REFERENCE_FIELDS = (
    "document_id",
    "document_name",
    "chunk_index",
    "source_chunk_id",
    "external_metadata",
)


def test_index_schema_defaults_reference_fields_to_none():
    # Non-chunk data points omit these fields; they must default to None,
    # never raise, so the schema stays compatible with every DataPoint type.
    instance = IndexSchema(id="11111111-1111-1111-1111-111111111111", text="hello")
    dumped = instance.model_dump()
    for field in REFERENCE_FIELDS:
        assert field in dumped, f"IndexSchema is missing {field}"
        assert dumped[field] is None


def test_index_schema_round_trips_reference_fields():
    instance = IndexSchema(
        id="11111111-1111-1111-1111-111111111111",
        text="some chunk text",
        document_id="22222222-2222-2222-2222-222222222222",
        document_name="annual_report.pdf",
        chunk_index=4,
        source_chunk_id="33333333-3333-3333-3333-333333333333",
        importance_weight=0.8,
        external_metadata='{"created_at": "2024-01-15"}',
    )
    dumped = instance.model_dump()
    assert dumped["document_id"] == "22222222-2222-2222-2222-222222222222"
    assert dumped["document_name"] == "annual_report.pdf"
    assert dumped["chunk_index"] == 4
    assert dumped["source_chunk_id"] == "33333333-3333-3333-3333-333333333333"
    assert dumped["importance_weight"] == 0.8
    assert dumped["external_metadata"] == '{"created_at": "2024-01-15"}'
