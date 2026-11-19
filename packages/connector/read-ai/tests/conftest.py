"""Disable upstream diagnostics before package imports; tests own temporary storage."""

import os

os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
os.environ["DO_NOT_TRACK"] = "1"
os.environ["TELEMETRY_DISABLED"] = "true"
os.environ["COGNEE_LOG_FILE"] = "false"
