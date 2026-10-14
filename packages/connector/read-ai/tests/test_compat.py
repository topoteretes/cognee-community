"""Check foreground completion at the pinned Cognee compatibility boundary."""

from uuid import uuid4

import pytest

from cognee_community_connector_read_ai import compat
from cognee_community_connector_read_ai.models import IncompleteSyncError


@pytest.mark.parametrize("status", ["started", "empty", "another_dataset"])
async def test_cognify_rejects_unfinished_or_missing_dataset(monkeypatch, status):
    import cognee
    from cognee.modules.pipelines.models import PipelineRunCompleted, PipelineRunStarted

    dataset_id = uuid4()
    fields = dict(pipeline_run_id=uuid4(), dataset_id=dataset_id, dataset_name="dataset")
    result = {
        "started": {dataset_id: PipelineRunStarted(**fields)},
        "empty": {},
        "another_dataset": {uuid4(): PipelineRunCompleted(**fields)},
    }[status]

    async def cognify(**kwargs):
        assert kwargs == dict(
            datasets=[dataset_id], user=None, run_in_background=False, raise_on_error=True
        )
        return result

    monkeypatch.setattr(cognee, "cognify", cognify)
    with pytest.raises(IncompleteSyncError, match="completed dataset run"):
        await compat.cognify(dataset_id, None)


@pytest.mark.parametrize("string_key", [False, True])
async def test_cognify_accepts_verified_completion(monkeypatch, string_key):
    import cognee
    from cognee.modules.pipelines.models import PipelineRunAlreadyCompleted

    dataset_id = uuid4()
    result = {
        str(dataset_id) if string_key else dataset_id: PipelineRunAlreadyCompleted(
            pipeline_run_id=uuid4(), dataset_id=dataset_id, dataset_name="dataset"
        )
    }

    async def cognify(**kwargs):
        return result

    monkeypatch.setattr(cognee, "cognify", cognify)
    assert await compat.cognify(dataset_id, None) is result
