"""Cognee Atlassian Opsgenie connector."""

from cognee_community_connector_opsgenie.client import (
    OpsgenieAuthError,
    OpsgenieClient,
    OpsgenieError,
    OpsgenieNotFoundError,
)
from cognee_community_connector_opsgenie.opsgenie import (
    OPSGENIE_ALERTS_TABLE_NAME,
    OPSGENIE_ESCALATIONS_TABLE_NAME,
    OPSGENIE_SCHEDULES_TABLE_NAME,
    OPSGENIE_SOURCE_NAME,
    opsgenie_source,
)

__all__ = [
    "OPSGENIE_ALERTS_TABLE_NAME",
    "OPSGENIE_ESCALATIONS_TABLE_NAME",
    "OPSGENIE_SCHEDULES_TABLE_NAME",
    "OPSGENIE_SOURCE_NAME",
    "OpsgenieAuthError",
    "OpsgenieClient",
    "OpsgenieError",
    "OpsgenieNotFoundError",
    "opsgenie_source",
]
