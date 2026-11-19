"""In-memory stand-in for the Amplitude REST API, shaped like its real responses."""

from cognee_community_connector_amplitude.amplitude import AmplitudeNotFoundError

EVENTS = "/api/2/taxonomy/event"
EVENT_PROPERTIES = "/api/2/taxonomy/event-property"
USER_PROPERTIES = "/api/2/taxonomy/user-property"
COHORTS = "/api/3/cohorts"
ANNOTATIONS = "/api/3/annotations"

_CLAUSE = {
    "negated": False,
    "orClauses": [
        {
            "type": "event",
            "type_value": "Checkout Completed",
            "operator": ">=",
            "operator_value": 1,
            "time_type": "rolling",
            "time_value": 30,
            "interval": 1,
            "offset": 0,
            "exclude_current_interval": False,
            "group_by": [],
            "metric": None,
        }
    ],
}
BEHAVIORAL_DEFINITION = {
    "version": 3,
    "countGroup": {"name": "User", "is_computed": False},
    "cohortType": "UNIQUES",
    "andClauses": [_CLAUSE],
    "orClauses": [[_CLAUSE]],
    "referenceFrameTimeParams": {},
}
# what an uploaded id-list cohort carries: no rules at all
UPLOAD_DEFINITION = {
    key: value for key, value in BEHAVIORAL_DEFINITION.items() if key != "orClauses"
} | {"andClauses": []}


def _cell(text: object) -> str:
    return f'"\t{text}"'


def _export(chart: dict) -> str:
    """A chart's CSV export: a header of single-cell lines, then the result table."""
    days = [f"2026-10-{day:02d}" for day in range(1, len(chart["values"]) + 1)]
    lines = [
        _cell(chart["title"]),
        "",
        *[_cell(line) for line in chart["definition"]],
        "",
        ",".join(_cell(cell) for cell in ["Segment", *days]),
        ",".join([_cell("All Users"), *[f'"{value}"' for value in chart["values"]]]),
    ]
    return "\r\n".join(lines) + "\r\n"


class FakeAmplitude:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.event_properties: dict[str, dict[str, dict]] = {}
        self.user_properties: dict[str, dict] = {}
        self.cohorts: dict[str, dict] = {}
        self.annotations: dict[int, dict] = {}
        self.charts: dict[str, dict] = {}
        # canned payloads returned instead of the real listing, to fake a broken answer
        self.overrides: dict[str, object] = {}
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None
        self.fail_paths: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict | None]] = []

    # -- seeding -----------------------------------------------------------
    def add_event(self, event_type: str, **fields) -> dict:
        self.events[event_type] = {
            "event_type": event_type,
            "category": None,
            "description": "",
            "display_name": None,
            "is_active": True,
            "is_hidden_from_dropdowns": False,
            "is_hidden_from_persona_results": False,
            "is_hidden_from_pathfinder": False,
            "is_hidden_from_timeline": False,
            "owner": "",
            "tags": [],
            "deleted": None,
            **fields,
        }
        self.event_properties.setdefault(event_type, {})
        return self.events[event_type]

    def add_event_property(self, event_type: str, name: str, **fields) -> dict:
        prop = {
            "event_property": name,
            "event_type": event_type,
            "description": "",
            "type": "any",
            "regex": None,
            "enum_values": None,
            "is_array_type": False,
            "is_required": False,
            "is_hidden": False,
            "classifications": [],
            **fields,
        }
        self.event_properties[event_type][name] = prop
        return prop

    def add_user_property(self, name: str, **fields) -> dict:
        self.user_properties[name] = {
            "user_property": name,
            "description": "",
            "type": "any",
            "enum_values": None,
            "regex": None,
            "is_array_type": False,
            "is_hidden": False,
            "classifications": [],
            "deleted": False,
            **fields,
        }
        return self.user_properties[name]

    def add_cohort(self, cohort_id: str, name: str, **fields) -> dict:
        self.cohorts[cohort_id] = {
            "id": cohort_id,
            "appId": 100001,
            "name": name,
            "description": None,
            "owners": ["owner@example.com"],
            "viewers": [],
            "type": "redshift",
            "published": True,
            "archived": False,
            "hidden": False,
            "finished": True,
            "definition": BEHAVIORAL_DEFINITION,
            "chart_id": None,
            # volatile bookkeeping the connector must never render
            "size": 4242,
            "lastComputed": 1791474254,
            "lastMod": 1791474254,
            "createdAt": 1791474253,
            "view_count": 17,
            "popularity": 3,
            "last_viewed": 1791474999,
            **fields,
        }
        return self.cohorts[cohort_id]

    def add_annotation(self, annotation_id: int, label: str, **fields) -> dict:
        self.annotations[annotation_id] = {
            "id": annotation_id,
            "label": label,
            "start": "2026-10-01T00:00:00+00:00",
            "end": None,
            "details": None,
            "category": {"id": 1, "category": "Uncategorized"},
            "chart_id": None,
            **fields,
        }
        return self.annotations[annotation_id]

    def add_chart(self, chart_id: str, title: str, *definition: str) -> dict:
        self.charts[chart_id] = {
            "title": title,
            "definition": list(definition),
            "values": [7001, 7002, 7003],
        }
        return self.charts[chart_id]

    def recompute(self) -> None:
        """Move every volatile field, the way a day of new data does."""
        for chart in self.charts.values():
            chart["values"] = [*chart["values"], 7999]
        for cohort in self.cohorts.values():
            cohort["size"] += 100
            cohort["lastComputed"] += 86_400
            cohort["lastMod"] += 86_400
            cohort["view_count"] += 1
            cohort["last_viewed"] += 86_400

    # -- the api -----------------------------------------------------------
    def count(self, path: str) -> int:
        return sum(1 for called, _ in self.calls if called == path)

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append((path, params))
        if path in self.fail_paths:
            raise self.fail_paths[path]
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with
        if path in self.overrides:
            return self.overrides[path]
        if path == EVENTS:
            # the real listing comes back in no stable order
            return {"success": True, "data": [dict(e) for e in reversed(self.events.values())]}
        if path == EVENT_PROPERTIES:
            properties = self.event_properties.get((params or {}).get("event_type"), {})
            return {"success": True, "data": [dict(p) for p in reversed(properties.values())]}
        if path == USER_PROPERTIES:
            return {"success": True, "data": [dict(p) for p in self.user_properties.values()]}
        if path == COHORTS:
            return {"cohorts": [dict(c) for c in self.cohorts.values()]}
        if path == ANNOTATIONS:
            return {"data": [dict(a) for a in self.annotations.values()]}
        if path.startswith("/api/3/chart/") and path.endswith("/csv"):
            chart = self.charts.get(path.split("/")[4])
            if chart is None:
                raise AmplitudeNotFoundError("Amplitude record not found (HTTP 404)")
            return {"data": _export(chart)}
        raise AssertionError(f"unexpected call GET {path}")
