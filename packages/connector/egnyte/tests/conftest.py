import pytest


@pytest.fixture(autouse=True)
def isolated_dlt_pipelines(tmp_path, monkeypatch):
    """Give every test its own dlt working directory and staging database.

    dlt keeps each pipeline's state (our event cursor and file map) under
    ``~/.dlt/pipelines``, and ``cognee.prune`` does not clear it.  Without this,
    a pipeline would resume from an earlier test run's cursor and sync nothing
    into the freshly pruned dataset.

    cognee also caches the dlt staging destination per dataset name, so a test
    that reuses a dataset name with a new ``tmp_path`` would load rows into the
    previous test's database and read back none.
    """
    dlt = pytest.importorskip("dlt")
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination

    pipeline_factory = dlt.pipeline
    monkeypatch.setattr(
        dlt,
        "pipeline",
        lambda *args, **kwargs: pipeline_factory(
            *args, pipelines_dir=str(tmp_path / "dlt_pipelines"), **kwargs
        ),
    )
    get_dlt_destination.cache_clear()
    yield
    get_dlt_destination.cache_clear()
