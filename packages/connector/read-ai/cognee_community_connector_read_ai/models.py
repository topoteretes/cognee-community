"""Small shared contracts for a bounded, explicitly acknowledged sync."""

from dataclasses import asdict, dataclass, field
from typing import Any


class ReadAIError(Exception):
    """A sync could not establish its guarantees; success was not acknowledged."""


class AuthenticationError(ReadAIError):
    """Credentials require renewal or reauthorization."""


class AccountMismatchError(AuthenticationError):
    """The credentials do not belong to the bound issuer/subject."""


class IncompleteSyncError(ReadAIError):
    """Upstream extraction, staging, graph construction, or verification was incomplete."""


class ConfigurationError(ReadAIError):
    """The source is invalid or differs from its stored configuration."""


class BudgetExceededError(IncompleteSyncError):
    """A configured bound was reached; the corpus was not truncated."""


class VerificationError(IncompleteSyncError):
    """The stored corpus does not match the prepared corpus."""


class UnsupportedBackendError(ConfigurationError):
    """The active backend cannot provide the verified v1 contract."""


@dataclass(frozen=True)
class SyncLimits:
    max_meetings: int = 1000
    max_pages: int = 300
    max_response_bytes: int = 4 * 1024 * 1024
    max_total_response_bytes: int = 128 * 1024 * 1024
    max_duration_seconds: float = 3600

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ConfigurationError(f"{name} must be positive and finite")
            import math

            if not math.isfinite(value):
                raise ConfigurationError(f"{name} must be positive and finite")
            if name != "max_duration_seconds" and not isinstance(value, int):
                raise ConfigurationError(f"{name} must be an integer")


@dataclass(frozen=True)
class Selection:
    meeting_ids: tuple[str, ...] | None = None
    start_time_ms: int | None = None
    end_time_ms: int | None = None
    include_summary: bool = True
    include_transcript: bool = True
    include_metrics: bool = True

    def __post_init__(self):
        if self.meeting_ids is not None:
            if not self.meeting_ids:
                raise ConfigurationError("meeting_ids cannot be empty")
            if self.start_time_ms is not None or self.end_time_ms is not None:
                raise ConfigurationError("meeting_ids and date bounds are mutually exclusive")
        for flag in (self.include_summary, self.include_transcript, self.include_metrics):
            if not isinstance(flag, bool):
                raise ConfigurationError("Content selections must be booleans")
        if not any((self.include_summary, self.include_transcript, self.include_metrics)):
            raise ConfigurationError("Select at least one content section")
        for value in (self.start_time_ms, self.end_time_ms):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ConfigurationError("Date bounds must be nonnegative milliseconds")
        if (
            self.start_time_ms is not None
            and self.end_time_ms is not None
            and self.start_time_ms >= self.end_time_ms
        ):
            raise ConfigurationError("The start must precede the exclusive end")


@dataclass
class Snapshot:
    meetings: dict[str, dict[str, Any]]
    rows: dict[str, dict[str, Any]]
    pending: list[str]
    identity: Any


@dataclass(frozen=True)
class SyncResult:
    created: int
    changed: int
    unchanged: int
    forgotten: int
    pending: int
    generation: str
    dataset_id: str
    reconciled: bool = True
    withdrawn: int = 0
    unavailable_sections: int = 0
    data_ids: tuple[str, ...] = field(default_factory=tuple)
