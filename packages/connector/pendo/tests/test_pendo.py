"""Offline tests for the Pendo connector's ingest and sync behavior."""

import pytest

from cognee_community_connector_pendo.pendo import (
    PENDO_SOURCE_NAME,
    PendoClient,
    _feedback_to_entity,
    _iter_rows,
    _PendoConfig,
    _poll_event_id,
    _survey_to_entity,
    pendo_source,
)


def _guide(guide_id: str, updated: int, name: str = "Onboarding", **extra):
    return {
        "id": guide_id,
        "guideName": name,
        "guideDescription": "Help customers get started.",
        "guideContent": "Connect your first data source.",
        "guidePermalink": f"https://app.pendo.io/guides/{guide_id}",
        "lastUpdatedAt": updated,
        **extra,
    }


def _poll(
    response: str,
    timestamp: int,
    *,
    guide_id: str = "g1",
    poll_id: str = "p1",
    visitor_id: str = "v1",
):
    return {
        "guideId": guide_id,
        "pollId": poll_id,
        "visitorId": visitor_id,
        "accountId": "a1",
        "browserTime": timestamp,
        "pollResponse": response,
    }


def _survey(response_id: str, text: str, timestamp: int):
    return {
        "id": response_id,
        "surveyId": "survey-1",
        "surveyName": "Quarterly NPS",
        "surveyType": "NET_PROMOTER_SCORE",
        "submittedAt": timestamp,
        "score": 9,
        "answers": [
            {
                "questionType": "RATING_SCALE",
                "questionText": "How likely are you to recommend us?",
                "text": "9",
                "value": 9,
            },
            {
                "questionType": "OPEN_ANSWER",
                "questionText": "Why?",
                "text": text,
                "value": None,
            },
        ],
    }


class FakePendoClient:
    def __init__(self, guides=None, feedback=None, polls=None, surveys=None):
        self.guides = list(guides or [])
        self.feedback = list(feedback or [])
        self.polls = list(polls or [])
        self.surveys = dict(surveys or {})
        self.calls = []

    def list_guides(self, *, app_id=None):
        self.calls.append(("guides", app_id))
        return list(self.guides)

    def list_poll_responses(self, *, start_time_ms=0, guide_ids=()):
        self.calls.append(("polls", start_time_ms, frozenset(guide_ids)))
        selected = set(guide_ids)
        return [item for item in self.polls if not selected or item.get("guideId") in selected]

    def list_feedback(self, *, app_id=None):
        self.calls.append(("feedback", app_id))
        return list(self.feedback)

    def list_survey_responses(self, survey_id, *, start_time_ms=0):
        self.calls.append(("survey", survey_id, start_time_ms))
        return list(self.surveys.get(survey_id, []))


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class RecordingHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return _Response(self.responses.pop(0))


def _config(**overrides):
    values = {
        "content_types": frozenset({"guides", "feedback", "nps"}),
        "app_id": None,
        "guide_ids": frozenset(),
        "nps_survey_ids": ("survey-1",),
        "start_time_ms": 0,
    }
    values.update(overrides)
    return _PendoConfig(**values)


def test_client_configures_integration_key_header():
    client = PendoClient("integration-secret")
    try:
        assert client._http.headers["x-pendo-integration-key"] == "integration-secret"
    finally:
        client._http.close()


def test_listen_feedback_uses_documented_vocitems_snapshot():
    http = RecordingHttpClient([{"results": [{"id": "feedback-1"}]}])
    client = PendoClient("test", http_client=http)

    assert client.list_feedback(app_id="app-1") == [{"id": "feedback-1"}]
    payload = http.requests[0][2]["json"]
    source = payload["request"]["pipeline"][0]["source"]
    assert source == {"vocItems": {"type": "feedback", "appId": ["app-1"]}}


def test_sentiment_pagination_follows_after_key():
    survey = {"id": "survey-1", "name": "NPS", "type": "NET_PROMOTER_SCORE"}
    http = RecordingHttpClient(
        [
            {
                "survey": survey,
                "responses": [{"id": "r1", "submittedAt": 1}],
                "hasMore": True,
                "afterKey": "next-page",
            },
            {
                "survey": survey,
                "responses": [{"id": "r2", "submittedAt": 2}],
                "hasMore": False,
            },
        ]
    )
    client = PendoClient("test", http_client=http)

    responses = client.list_survey_responses("survey-1")

    assert [response["id"] for response in responses] == ["r1", "r2"]
    assert "afterKey" not in http.requests[0][2]["params"]
    assert http.requests[1][2]["params"]["afterKey"] == "next-page"


def test_ingest_keeps_knowledge_text_and_drops_usage_metrics():
    client = FakePendoClient(
        guides=[_guide("g1", 100), _guide("nps-guide", 100, name="NPS", recurrence=1)],
        feedback=[
            {
                "id": "listen-1",
                "lastUpdatedAt": 105,
                "formData": [
                    {"title": "Title", "type": "short text", "content": "Bulk editing"},
                    {
                        "title": "What problem are you trying to solve?",
                        "type": "long text",
                        "content": "Editing many records takes too long.",
                    },
                ],
                "importance": 2,
            }
        ],
        polls=[
            _poll("The import needs clearer errors.", 110),
            _poll("10", 120, visitor_id="numeric-only"),
            _poll(
                "Support was exceptionally helpful.",
                130,
                guide_id="nps-guide",
                visitor_id="nps-poll",
            ),
        ],
        surveys={"survey-1": [_survey("nps-1", "Setup was delightfully quick.", 140)]},
    )

    rows = list(_iter_rows(client, _config(), {}))
    live = [row for row in rows if not row["_deleted"]]

    assert {row["id"] for row in live if row["id"].startswith("guide:")} == {
        "guide:g1",
        "guide:nps-guide",
    }
    assert any("Editing many records takes too long." in row["content"] for row in live)
    assert any(row["content"] == "The import needs clearer errors." for row in live)
    assert any(row["content"] == "Support was exceptionally helpful." for row in live)
    assert any("Setup was delightfully quick." in row["content"] for row in live)
    # Scores and numeric-only poll responses are analytics, not documents.
    assert not any(row["content"] in {"9", "10"} for row in live)
    assert all("score" not in row for row in live)


def test_incremental_cursor_emits_only_newer_or_new_entities():
    client = FakePendoClient(
        guides=[_guide("g1", 100)],
        polls=[_poll("Initial feedback", 110)],
        surveys={"survey-1": [_survey("nps-1", "Initial NPS text", 120)]},
    )
    state = {}
    first = list(_iter_rows(client, _config(), state))
    assert len(first) == 3
    assert state["last_updated_at"] == {"feedback": 110, "guides": 100, "nps": 120}

    # Same inventory and timestamps: no row is sent back through ingestion.
    assert list(_iter_rows(client, _config(), state)) == []

    # Only g1 changed. A brand-new response at the cursor timestamp is still
    # emitted because its ID was not in the previous inventory.
    client.guides[0] = _guide("g1", 200, name="Updated onboarding")
    client.surveys["survey-1"].append(_survey("nps-2", "Same-time new response", 120))
    changed = list(_iter_rows(client, _config(), state))

    assert {row["id"] for row in changed} == {"guide:g1", "nps:nps-2"}
    assert state["last_updated_at"]["guides"] == 200


def test_default_sentiment_inventory_boundary_is_persisted():
    client = FakePendoClient(
        guides=[],
        surveys={"survey-1": [_survey("nps-1", "Keep this response", 120)]},
    )
    state = {}

    list(_iter_rows(client, _config(content_types=frozenset({"nps"})), state))
    pinned_start = state["sentiment_start_time_ms"]
    list(_iter_rows(client, _config(content_types=frozenset({"nps"})), state))

    survey_calls = [call for call in client.calls if call[0] == "survey"]
    assert pinned_start > 0
    assert survey_calls == [
        ("survey", "survey-1", pinned_start),
        ("survey", "survey-1", pinned_start),
    ]


def test_upstream_deletion_emits_hard_delete_tombstone():
    poll = _poll("Please improve bulk editing.", 110)
    client = FakePendoClient(guides=[_guide("g1", 100)], polls=[poll])
    config = _config(
        content_types=frozenset({"feedback"}),
        nps_survey_ids=(),
    )
    state = {}
    first = list(_iter_rows(client, config, state))
    feedback_id = f"feedback:{_poll_event_id(poll)}"
    assert [row["id"] for row in first] == [feedback_id]

    client.polls = []
    assert list(_iter_rows(client, config, state)) == [{"id": feedback_id, "_deleted": True}]
    assert state["known_ids"]["feedback"] == []


def test_changing_selection_forgets_previously_selected_kind():
    client = FakePendoClient(guides=[_guide("g1", 100)], polls=[_poll("Good idea", 110)])
    state = {}
    list(_iter_rows(client, _config(nps_survey_ids=()), state))

    rows = list(
        _iter_rows(
            client,
            _config(content_types=frozenset({"guides"}), nps_survey_ids=()),
            state,
        )
    )
    assert any(row["id"].startswith("feedback:") and row["_deleted"] for row in rows)
    assert state["known_ids"]["feedback"] == []


def test_nps_document_contains_only_open_answer_text():
    row = _survey_to_entity(_survey("nps-1", "I love the workflows.", 100))
    assert row is not None
    assert row["content"] == "Why?\nI love the workflows."
    assert "9" not in row["content"]


def test_listen_feedback_uses_form_text_not_importance_metrics():
    row = _feedback_to_entity(
        {
            "id": "f1",
            "lastUpdatedAt": 200,
            "importance": 2,
            "impact": 5,
            "formData": [
                {"title": "Title", "content": "CSV imports"},
                {"title": "Problem", "content": "Quoted commas break my uploads."},
                {"title": "Rating", "content": "5"},
            ],
        }
    )
    assert row is not None
    assert row["title"] == "CSV imports"
    assert "Quoted commas break my uploads." in row["content"]
    assert "Rating" not in row["content"]
    assert "importance" not in row


def test_source_validates_selection_and_declares_document_mode():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    with pytest.raises(ValueError, match="Unsupported"):
        pendo_source(client=FakePendoClient(), content_types=["metrics"])

    source = pendo_source(client=FakePendoClient(), content_types=["guides"])
    assert document_source_tag(source) == PENDO_SOURCE_NAME


def test_dlt_merge_ingests_and_physically_deletes_row(tmp_path):
    dlt = pytest.importorskip("dlt")
    client = FakePendoClient(guides=[_guide("g1", 100), _guide("g2", 100)])
    pipeline = dlt.pipeline(
        pipeline_name="pendo_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'pendo.db'}"),
        dataset_name="pendo_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    pipeline.run(pendo_source(client=client, content_types=["guides"]))
    client.guides = [_guide("g2", 100)]
    pipeline.run(pendo_source(client=client, content_types=["guides"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM pendo_content") as cursor,
    ):
        ids = {row[0] for row in cursor.fetchall()}
    assert ids == {"guide:g2"}
