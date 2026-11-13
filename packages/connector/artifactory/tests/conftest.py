"""Keep offline tests independent of DLT's external telemetry endpoint."""

import os

# Set before test collection imports DLT. Its telemetry executor otherwise waits
# for network retries during process shutdown, even after all tests have passed.
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
