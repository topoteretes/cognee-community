def test_package_version():
    import cognee_community_connector_gitgraph as pkg

    assert pkg.__version__ == "0.1.0"


def test_runtime_dependencies_import():
    import cognee
    import dlt
    import git
    import networkx

    assert all(m is not None for m in (cognee, dlt, git, networkx))


def test_cognee_document_mode_api():
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, document_source_tag
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    assert DOCUMENT_SOURCE_ATTR == "cognee_document_source"
    assert callable(document_source_tag)
    assert callable(_build_document_data_item)
