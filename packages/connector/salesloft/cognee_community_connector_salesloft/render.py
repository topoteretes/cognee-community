"""Render Salesloft records into readable document text with provenance.

Each record becomes one cognee document with:
- Deterministic ID: ``salesloft:{type}:{id}``
- Human-readable markdown content
- Provenance metadata (Salesloft ID, type, owner, person, account, etc.)
- ``cognee_node_set`` entries for scoped recall
"""

from __future__ import annotations

from typing import Any


def render_note(note: dict[str, Any]) -> dict[str, Any]:
    """Render a Salesloft note to a document row."""
    note_id = note.get("id")
    content_text = (note.get("content") or "").strip()

    # Resolve related entity names for context.
    person = note.get("associated_with") or {}
    person_name = _person_display(person)
    user = note.get("user") or {}
    user_name = user.get("name") or ""

    title = f"Note on {person_name}" if person_name else "Note"
    if user_name:
        title += f" by {user_name}"

    # Build readable document.
    lines = [f"# {title}"]
    if content_text:
        lines.append(content_text)

    return _build_row(
        record_id=note_id,
        resource_type="note",
        title=title,
        content="\n\n".join(lines),
        updated_at=note.get("updated_at", ""),
        provenance=_note_provenance(note),
        node_set=_note_node_set(note),
    )


def render_call(call: dict[str, Any]) -> dict[str, Any]:
    """Render a Salesloft call activity to a document row."""
    call_id = call.get("id")
    note = (call.get("note") or "").strip()
    disposition = (call.get("disposition") or "").strip()
    sentiment = (call.get("sentiment") or "").strip()
    duration = call.get("duration") or 0
    direction = (call.get("direction") or "").strip()

    # Resolve related entities.
    person = call.get("called_person") or call.get("user") or {}
    person_name = _person_display(person)
    user = call.get("user") or {}
    user_name = user.get("name") or ""

    title = f"Call with {person_name}" if person_name else "Call"
    if user_name and user_name != person_name:
        title += f" by {user_name}"

    lines = [f"# {title}"]
    meta_parts = []
    if direction:
        meta_parts.append(f"Direction: {direction}")
    if duration:
        meta_parts.append(f"Duration: {duration}s")
    if disposition:
        meta_parts.append(f"Disposition: {disposition}")
    if sentiment:
        meta_parts.append(f"Sentiment: {sentiment}")
    if meta_parts:
        lines.append(" | ".join(meta_parts))
    if note:
        lines.append(f"## Notes\n{note}")

    return _build_row(
        record_id=call_id,
        resource_type="call",
        title=title,
        content="\n\n".join(lines),
        updated_at=call.get("updated_at", ""),
        provenance=_call_provenance(call),
        node_set=_call_node_set(call),
    )


def render_cadence(cadence: dict[str, Any]) -> dict[str, Any]:
    """Render a Salesloft cadence to a document row."""
    cadence_id = cadence.get("id")
    name = (cadence.get("name") or "").strip()

    owner = cadence.get("owner") or cadence.get("creator") or {}
    owner_name = owner.get("name") or ""
    target_daily = cadence.get("target_daily_people") or ""
    cadence_type = (cadence.get("cadence_function") or "").strip()

    title = f"Cadence: {name}" if name else "Cadence"

    lines = [f"# {title}"]
    meta_parts = []
    if owner_name:
        meta_parts.append(f"Owner: {owner_name}")
    if cadence_type:
        meta_parts.append(f"Type: {cadence_type}")
    if target_daily:
        meta_parts.append(f"Daily target: {target_daily}")
    if meta_parts:
        lines.append(" | ".join(meta_parts))

    # Steps / persona if available.
    counts = cadence.get("counts", {})
    if counts:
        step_parts = []
        for key, val in counts.items():
            if val:
                step_parts.append(f"{key}: {val}")
        if step_parts:
            lines.append("## Counts\n" + "\n".join(f"- {s}" for s in step_parts))

    return _build_row(
        record_id=cadence_id,
        resource_type="cadence",
        title=title,
        content="\n\n".join(lines),
        updated_at=cadence.get("updated_at", ""),
        provenance=_cadence_provenance(cadence),
        node_set=_cadence_node_set(cadence),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_row(
    *,
    record_id: int | str | None,
    resource_type: str,
    title: str,
    content: str,
    updated_at: str,
    provenance: dict[str, Any],
    node_set: list[str],
) -> dict[str, Any]:
    """Build the standard document row for dlt ingestion."""
    stable_id = f"salesloft:{resource_type}:{record_id}"
    return {
        "id": stable_id,
        "title": title,
        "content": content,
        "updated_at": updated_at,
        "provenance": provenance,
        "cognee_node_set": node_set,
        "_deleted": False,
    }


def deleted_row(record_id: int | str, resource_type: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a record by id."""
    stable_id = f"salesloft:{resource_type}:{record_id}"
    return {"id": stable_id, "_deleted": True}


def _person_display(person: dict[str, Any]) -> str:
    """Extract a human-readable name from a person/user reference."""
    if not person:
        return ""
    name = person.get("name") or person.get("display_name") or ""
    if name:
        return name
    first = person.get("first_name") or ""
    last = person.get("last_name") or ""
    return f"{first} {last}".strip()


def _nested_id(obj: dict[str, Any] | None, key: str = "id") -> int | str | None:
    """Safely extract an id from a nested Salesloft reference object."""
    if isinstance(obj, dict):
        return obj.get(key) or obj.get("_href", "").rstrip("/").split("/")[-1] or None
    return None


# ---------------------------------------------------------------------------
# Provenance builders
# ---------------------------------------------------------------------------


def _note_provenance(note: dict[str, Any]) -> dict[str, Any]:
    prov: dict[str, Any] = {
        "salesloft_id": note.get("id"),
        "resource_type": "note",
        "updated_at": note.get("updated_at"),
    }
    user = note.get("user")
    if user:
        prov["owner_id"] = _nested_id(user)
    associated = note.get("associated_with")
    if associated:
        prov["person_id"] = _nested_id(associated)
    return prov


def _call_provenance(call: dict[str, Any]) -> dict[str, Any]:
    prov: dict[str, Any] = {
        "salesloft_id": call.get("id"),
        "resource_type": "call",
        "updated_at": call.get("updated_at"),
        "duration": call.get("duration"),
        "disposition": call.get("disposition"),
        "direction": call.get("direction"),
    }
    user = call.get("user")
    if user:
        prov["owner_id"] = _nested_id(user)
    person = call.get("called_person")
    if person:
        prov["person_id"] = _nested_id(person)
    account = call.get("account")
    if account:
        prov["account_id"] = _nested_id(account)
    cadence = call.get("cadence")
    if cadence:
        prov["cadence_id"] = _nested_id(cadence)
    return prov


def _cadence_provenance(cadence: dict[str, Any]) -> dict[str, Any]:
    prov: dict[str, Any] = {
        "salesloft_id": cadence.get("id"),
        "resource_type": "cadence",
        "updated_at": cadence.get("updated_at"),
        "archived_at": cadence.get("archived_at"),
    }
    owner = cadence.get("owner") or cadence.get("creator")
    if owner:
        prov["owner_id"] = _nested_id(owner)
    return prov


# ---------------------------------------------------------------------------
# Node-set builders (for scoped recall)
# ---------------------------------------------------------------------------


def _note_node_set(note: dict[str, Any]) -> list[str]:
    nodes = [f"salesloft:note:{note.get('id')}"]
    person = note.get("associated_with")
    if person:
        pid = _nested_id(person)
        if pid:
            nodes.append(f"salesloft:person:{pid}")
    return nodes


def _call_node_set(call: dict[str, Any]) -> list[str]:
    nodes = [f"salesloft:call:{call.get('id')}"]
    person = call.get("called_person")
    if person:
        pid = _nested_id(person)
        if pid:
            nodes.append(f"salesloft:person:{pid}")
    account = call.get("account")
    if account:
        aid = _nested_id(account)
        if aid:
            nodes.append(f"salesloft:account:{aid}")
    cadence = call.get("cadence")
    if cadence:
        cid = _nested_id(cadence)
        if cid:
            nodes.append(f"salesloft:cadence:{cid}")
    return nodes


def _cadence_node_set(cadence: dict[str, Any]) -> list[str]:
    nodes = [f"salesloft:cadence:{cadence.get('id')}"]
    owner = cadence.get("owner") or cadence.get("creator")
    if owner:
        oid = _nested_id(owner)
        if oid:
            nodes.append(f"salesloft:user:{oid}")
    return nodes
