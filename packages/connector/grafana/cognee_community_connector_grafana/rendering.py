"""Allowlisted Grafana prose. Filtering is best effort, never a secret detector guarantee."""

import re
from urllib.parse import quote, quote_plus

from .client import GrafanaSyncError

# Shapes are deliberately bounded; ordinary dates, regions and short words survive.
_SHAPES = re.compile(
    r"\b(?:glsa_[A-Za-z0-9_=-]{12,}|(?:ghp|github_pat|sk)[_-][A-Za-z0-9_-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|[a-fA-F0-9]{30,64})\b"
)
# Match quoted JSON values as units, including escaped quotes and spaces.
_QUOTED = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
_VALUE = _QUOTED + r'|[^\s"\'&,;<>}]+'
# Parse parameter lists (e.g. Digest) before single scheme credentials. Bounded
# values preserve following incident clauses instead of swallowing the whole line.
_AUTH_SCHEME = r"[a-z][a-z0-9_-]*[ \t]+"
_AUTH_PARAM = rf"[a-z][a-z0-9_-]*[ \t]*=[ \t]*(?:{_VALUE})"
_AUTH_PARAMETERS = rf"{_AUTH_SCHEME}{_AUTH_PARAM}(?:[ \t]*,[ \t]*{_AUTH_PARAM})*"
_AUTHORIZATION = re.compile(
    r"(?i)\b((?:proxy[-_])?authorization[\"']?[ \t]*[:=][ \t]*)"
    rf"({_QUOTED}|{_AUTH_PARAMETERS}|{_AUTH_SCHEME}(?:{_VALUE})|{_VALUE})"
)
_LABEL = re.compile(
    r"(?i)\b((?:password|passwd|pwd|(?:db|database)[_-]?(?:password|passwd|pwd|pass)|"
    r"api[_-]?(?:key|token)|(?:access|refresh|auth)[_-]?token|client[_-]?secret)"
    r"[\"']?[ \t]*[:=][ \t]*|bearer[ \t]+)(" + _VALUE + r")"
)
# URI userinfo identifies credentials independently of protocol (including database
# and driver-qualified schemes); ordinary host/path URLs have no userinfo match.
_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@:\"'<>]*:[^\s/@\"'<>]+@", re.I)


def text(value, field):
    if not isinstance(value, str):
        raise GrafanaSyncError(f"Invalid Grafana {field}")
    return value


def tags(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise GrafanaSyncError("Invalid Grafana tags")
    return sorted(set(value))


def cleaner(values):
    # Explicit values only, not all values from omitted queries/options/variables.
    variants = set()
    for value in values:
        if len(value) >= 4:
            variants.update((value, quote(value, safe=""), quote_plus(value)))

    def clean(value):
        for secret in sorted(variants, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = _USERINFO.sub(r"\1[REDACTED]@", value)
        value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
        value = _LABEL.sub(r"\1[REDACTED]", value)
        return _SHAPES.sub("[REDACTED]", value)

    return clean


def panel_lines(panels, depth=0):
    if not isinstance(panels, list) or depth > 20:
        raise GrafanaSyncError("Unsupported Grafana panel structure/depth")
    lines = []
    for panel in panels:
        if not isinstance(panel, dict):
            raise GrafanaSyncError("Invalid Grafana panel")
        if panel.get("libraryPanel"):
            raise GrafanaSyncError(
                "Library panels require independent model sync and are unsupported"
            )
        title = text(panel.get("title", ""), "panel title")
        description = text(panel.get("description", ""), "panel description")
        lines.append(f"Panel: {title}\n{description}".strip())
        lines.extend(panel_lines(panel.get("panels", []), depth + 1))
    return lines


def dashboard_row(uid, item, origin, org, clean):
    spec = item["spec"]
    if spec.get("schemaVersion") != 42 or "elements" in spec or "layout" in spec:
        raise GrafanaSyncError("Only classic schemaVersion 42 dashboards are supported")
    folder = item["metadata"].get("annotations", {}).get("grafana.app/folder", "")
    folder = text(folder, "folder UID")
    url = f"{origin}/d/{quote(uid, safe='')}"
    content = "\n".join(
        [
            f"Grafana: {origin}; organization: {org}",
            f"Dashboard UID: {uid}",
            f"Folder UID: {folder or '(general)'}",
            f"URL: {url}",
            f"Tags: {', '.join(tags(spec.get('tags', [])))}",
            text(spec.get("description", ""), "dashboard description"),
            *panel_lines(spec.get("panels", [])),
        ]
    )
    return {
        "id": f"dashboard:{uid}",
        "title": clean(text(spec.get("title"), "dashboard title")),
        "content": clean(content),
    }


def annotation_row(annotation, origin, org, clean):
    uid = annotation.get("dashboardUID") or ""
    if not isinstance(uid, str):
        raise GrafanaSyncError("Invalid annotation dashboard UID")
    url = f"{origin}/d/{quote(uid, safe='')}" if uid else origin
    end = annotation.get("timeEnd", annotation["time"])
    content = "\n".join(
        [
            f"Grafana: {origin}; organization: {org}; annotation ID: {annotation['id']}",
            f"Dashboard UID: {uid or '(organization)'}; panel ID: {annotation.get('panelId', 0)}",
            f"URL: {url}",
            f"Time (epoch ms): {annotation['time']}; end: {end}",
            f"Tags: {', '.join(tags(annotation.get('tags', [])))}",
            text(annotation.get("text"), "annotation text"),
        ]
    )
    return {
        "id": f"annotation:{annotation['id']}",
        "title": f"Annotation {annotation['id']}",
        "content": clean(content),
    }
