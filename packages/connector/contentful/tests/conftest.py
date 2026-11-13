"""Disable outbound telemetry before Cognee/dlt are imported by offline tests."""

import os

os.environ["TELEMETRY_DISABLED"] = "true"
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
