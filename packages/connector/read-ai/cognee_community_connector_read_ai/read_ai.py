"""Document-mode source. Use sync_read_ai for the acknowledged lifecycle."""

import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .journal import digest
from .models import ConfigurationError, Selection, SyncLimits

SOURCE_NAME = "read_ai"


def rate_directory():
    """One local user's account quotas, independent of connections/installations."""
    default = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return _private_path(
        os.environ.get("READ_AI_RATE_DIR") or default / "cognee-read-ai" / "rates",
        "shared rate state",
    )


@dataclass(frozen=True)
class SourceConfig:
    connection_id: str
    token_provider: Any
    client: Any
    selection: Selection
    limits: SyncLimits
    state_dir: Path

    def fingerprint(self):
        # Deliberately retain end=None; the run's upper bound is not configuration.
        selection = asdict(self.selection)
        if selection["meeting_ids"] is not None:
            selection["meeting_ids"] = list(selection["meeting_ids"])
        return {"connection_id": self.connection_id, "selection": selection}


def _timestamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigurationError("Date bounds must be timezone-aware ISO 8601") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ConfigurationError("Date bounds must be timezone-aware datetimes or ISO 8601")
    return int(value.timestamp() * 1000)


def _private_path(value, label):
    resolved = Path(value).expanduser().resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise ConfigurationError(f"Keep {label} outside Git checkouts")
    return resolved


def read_ai_source(
    connection_id: str,
    *,
    credentials_path=None,
    token_provider=None,
    meeting_ids=None,
    start_time=None,
    end_time=None,
    include_summary=True,
    include_transcript=True,
    include_metrics=True,
    client=None,
    state_dir=None,
    limits=None,
):
    """Build a lazy dlt source; no network request or dataset mutation occurs here.

    Direct Cognee callers MUST supply dlt_config with merge/id and own recovery,
    authorization checks and acknowledgement. The required supported entrypoint
    is sync_read_ai(source, dataset_name=...).
    """
    from .auth import FileTokenProvider, default_credentials_path

    if not isinstance(connection_id, str) or not connection_id.strip():
        raise ConfigurationError("A stable, nonempty connection_id is required")
    if credentials_path is not None and token_provider is not None:
        raise ConfigurationError("Choose credentials_path or token_provider")
    if token_provider is None:
        token_provider = FileTokenProvider(
            _private_path(credentials_path or default_credentials_path(), "OAuth credentials"),
            client=client,
        )
    elif isinstance(token_provider, FileTokenProvider):
        _private_path(token_provider.path, "OAuth credentials")
    if limits is not None and not isinstance(limits, SyncLimits):
        raise ConfigurationError("limits must be a SyncLimits instance")
    if meeting_ids is not None:
        if isinstance(meeting_ids, (str, bytes)):
            raise ConfigurationError("meeting_ids must be a sequence of meeting ULIDs")
        if any(not isinstance(value, str) for value in meeting_ids):
            raise ConfigurationError("meeting_ids must contain strings")
        meeting_ids = tuple(sorted(set(meeting_ids)))
    selection = Selection(
        meeting_ids=meeting_ids,
        start_time_ms=_timestamp(start_time),
        end_time_ms=_timestamp(end_time),
        include_summary=include_summary,
        include_transcript=include_transcript,
        include_metrics=include_metrics,
    )
    config = SourceConfig(
        connection_id=connection_id,
        token_provider=token_provider,
        client=client,
        selection=selection,
        limits=limits or SyncLimits(),
        state_dir=_private_path(
            state_dir
            or os.environ.get("READ_AI_STATE_DIR")
            or Path.home() / ".local" / "state" / "cognee-read-ai",
            "synchronization state",
        ),
    )
    table_name = "read_ai_meetings_" + digest(config.fingerprint())[:24]
    return _make_source(config, table_name, table_name)


def _make_source(config, table_name, pipeline_scope, *, snapshot=None, rebuild=False):
    import dlt
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    @dlt.resource(
        name=table_name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "id": {"data_type": "text", "nullable": False},
            "title": {"data_type": "text"},
            "content": {"data_type": "text"},
            "url": {"data_type": "text"},
            "_deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def meetings():
        from .auth import OAuthIdentity
        from .client import ReadAIClient

        state = dlt.current.resource_state()
        current = snapshot
        if current is None:
            expected = OAuthIdentity(**state["identity"]) if state.get("identity") else None
            api = ReadAIClient(
                config.token_provider,
                client=config.client,
                limits=config.limits,
                rate_dir=rate_directory(),
            )
            try:
                current = api.reconcile(config.selection, set(state.get("known_ids", [])), expected)
            finally:
                api.close()
        previous = state.get("hashes", {})
        hashes = {key: digest(row) for key, row in current.rows.items()}
        for key, row in sorted(current.rows.items()):
            if rebuild or hashes[key] != previous.get(key):
                yield row
        if not rebuild:
            for key in sorted(set(previous) - set(hashes)):
                yield {"id": key, "title": "", "content": "", "url": "", "_deleted": True}
        # The empty replace recovery must carry the table/schema as a real load.
        if rebuild and not current.rows:
            yield []
        state["hashes"] = hashes
        state["known_ids"] = sorted(current.meetings)
        state["identity"] = asdict(current.identity)
        state["selection"] = asdict(config.selection)

    @dlt.source(name=SOURCE_NAME)
    def _source():
        return meetings

    source = _source()
    setattr(source, DOCUMENT_SOURCE_ATTR, SOURCE_NAME)
    setattr(source, PIPELINE_SCOPE_ATTR, pipeline_scope)
    source._read_ai_config = config
    source._read_ai_table = table_name
    return source
