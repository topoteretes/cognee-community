"""Signal policy for filtering Salesloft records.

Activity volume is very high relative to information content. This module
provides a configurable ``SignalPolicy`` that determines which records carry
enough signal to ingest (e.g. calls with notes, non-trivial durations).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SignalPolicy:
    """Configurable filter for Salesloft records.

    Attributes:
        min_call_seconds: Minimum call duration (seconds) to ingest.
            Calls shorter than this are dropped unless they have a note or
            a meaningful disposition.
        require_note_or_disposition: If True, calls without either a
            recording note or a non-empty disposition are dropped.
        excluded_dispositions: Dispositions to always exclude
            (e.g. "No Answer", "Busy").
        include_archived_cadences: Whether to include archived cadences.
    """

    min_call_seconds: int = 0
    require_note_or_disposition: bool = True
    excluded_dispositions: set[str] = field(default_factory=set)
    include_archived_cadences: bool = False

    def accept_call(self, call: dict[str, Any]) -> bool:
        """Return True if the call passes the signal filter."""
        duration = call.get("duration") or 0
        note = (call.get("note") or "").strip()
        disposition = (call.get("disposition") or "").strip()
        sentiment = (call.get("sentiment") or "").strip()

        # Always exclude explicitly blacklisted dispositions.
        if disposition and disposition in self.excluded_dispositions:
            return False

        # Short calls with no note and no disposition are noise.
        has_signal = bool(note) or bool(disposition) or bool(sentiment)

        if duration < self.min_call_seconds and not has_signal:
            return False

        return not (self.require_note_or_disposition and not has_signal)

    def accept_cadence(self, cadence: dict[str, Any]) -> bool:
        """Return True if the cadence passes the filter."""
        return not (not self.include_archived_cadences and cadence.get("archived_at"))

    def accept_note(self, note: dict[str, Any]) -> bool:
        """Notes are always accepted (they are human-authored signal)."""
        return True


# Sensible defaults for production use.
DEFAULT_POLICY = SignalPolicy(
    min_call_seconds=10,
    require_note_or_disposition=True,
    excluded_dispositions=set(),
    include_archived_cadences=False,
)
