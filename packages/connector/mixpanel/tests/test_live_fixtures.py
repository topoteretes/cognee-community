"""Sanitized synthetic-project responses captured on 1 November 2026.

No live requests run in these regression tests. Saved-report IDs are normalized.
"""

import json
from pathlib import Path

from cognee_community_connector_mixpanel.documents import document

FIXTURES = Path(__file__).parent / "fixtures"


def test_live_saved_funnel_preserves_server_parameters_without_migrations():
    report = json.loads((FIXTURES / "saved-funnel.json").read_text())
    row = document("report", report, "fixture-scope", "https://mixpanel.com/project/123")
    text = row["content"]
    assert "ConnectorTestSignup" in text and "ConnectorTestPurchase" in text
    assert '"conversionWindowDuration": 7' in text
    assert '"filterValue": [' in text and '"test"' in text
    assert '"funnelOrder": "loose"' in text
    assert "executedMigrations" not in text
    assert "creator" not in text


def test_live_event_schema_preserves_provider_normalized_metadata():
    schemas = json.loads((FIXTURES / "event-schemas.json").read_text())
    assert len(schemas) == 2
    for schema in schemas:
        row = document("schema", schema, "fixture-scope", "https://mixpanel.com/project/123")
        assert '"plan"' in row["content"] and '"type": "string"' in row["content"]
        assert '"hidden": false' in row["content"] and '"dropped": false' in row["content"]
        assert "connector-test" in row["content"]
