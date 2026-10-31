"""In-memory stand-in for the UptimeRobot API v2."""

from typing import Any

from cognee_community_connector_uptimerobot.uptimerobot import UptimeRobotNotFoundError


class FakeUptimeRobot:
    def __init__(self, base_url: str = "https://api.uptimerobot.com/v2"):
        self.base_url = base_url
        self.monitors: dict[str, dict[str, Any]] = {}
        self.psps: dict[str, dict[str, Any]] = {}
        self.alert_contacts: dict[str, dict[str, Any]] = {}
        self.mwindows: dict[str, dict[str, Any]] = {}

        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.fail_paths: dict[str, Exception] = {}
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None
        self.rate_limit: dict[str, int | None] = {}
        self._clock = 0

    def add_monitor(self, m_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": int(m_id) if m_id.isdigit() else m_id,
            "friendly_name": name,
            "url": fields.pop("url", "https://api.example.com"),
            "type": fields.pop("type", 1),
            "status": fields.pop("status", 2),  # 2: UP
            "interval": fields.pop("interval", 300),
            "custom_uptime_ratio": fields.pop("custom_uptime_ratio", "100.00-99.99-99.95"),
            "average_response_time": fields.pop("average_response_time", "125.40"),
            "http_username": fields.pop("http_username", "admin"),
            "http_password": fields.pop("http_password", "supersecretpass"),
            **fields,
        }
        self.monitors[str(m_id)] = rec
        return rec

    def add_psp(self, psp_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": int(psp_id) if psp_id.isdigit() else psp_id,
            "friendly_name": name,
            "custom_domain": fields.pop("custom_domain", "status.example.com"),
            "standard_url": fields.pop("standard_url", "https://stats.uptimerobot.com/abc"),
            "status": fields.pop("status", 1),
            "monitors": fields.pop("monitors", [101]),
            **fields,
        }
        self.psps[str(psp_id)] = rec
        return rec

    def add_alert_contact(self, c_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": c_id,
            "friendly_name": name,
            "type": fields.pop("type", 11),  # Slack
            "status": fields.pop("status", 2),  # Active
            "value": fields.pop("value", "https://hooks.slack.com/services/secretwebhookurl"),
            **fields,
        }
        self.alert_contacts[str(c_id)] = rec
        return rec

    def add_mwindow(self, mw_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": mw_id,
            "friendly_name": name,
            "duration": fields.pop("duration", 60),
            "value": fields.pop("value", "2026-10-25 02:00"),
            "status": fields.pop("status", 1),
            **fields,
        }
        self.mwindows[str(mw_id)] = rec
        return rec

    def request(self, endpoint: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", endpoint))
        self.bodies.append((endpoint, dict(body or {})))

        body = body or {}
        if endpoint in self.fail_paths:
            raise self.fail_paths[endpoint]
        for key in ("monitors", "psps", "alert_contacts", "mwindows"):
            if key in body and f"{endpoint}:{body[key]}" in self.fail_paths:
                raise self.fail_paths[f"{endpoint}:{body[key]}"]

        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with or RuntimeError("Simulated failure")
        offset = body.get("offset", 0)
        limit = body.get("limit", 50)

        # Single check lookups
        if (
            "monitors" in body
            and endpoint == "getMonitors"
            and not isinstance(body["monitors"], list)
        ):
            m_id = str(body["monitors"])
            if m_id in self.monitors:
                return {"stat": "ok", "monitors": [self.monitors[m_id]]}
            raise UptimeRobotNotFoundError(f"Monitor {m_id} not found")

        if "psps" in body and endpoint == "getPSPs":
            p_id = str(body["psps"])
            if p_id in self.psps:
                return {"stat": "ok", "psps": [self.psps[p_id]]}
            raise UptimeRobotNotFoundError(f"PSP {p_id} not found")

        if "alert_contacts" in body and endpoint == "getAlertContacts":
            ac_id = str(body["alert_contacts"])
            if ac_id in self.alert_contacts:
                return {"stat": "ok", "alert_contacts": [self.alert_contacts[ac_id]]}
            raise UptimeRobotNotFoundError(f"Alert contact {ac_id} not found")

        if "mwindows" in body and endpoint == "getMWindows":
            mw_id = str(body["mwindows"])
            if mw_id in self.mwindows:
                return {"stat": "ok", "mwindows": [self.mwindows[mw_id]]}
            raise UptimeRobotNotFoundError(f"MWindow {mw_id} not found")

        # Listings
        if endpoint == "getMonitors":
            records = list(self.monitors.values())
            return {
                "stat": "ok",
                "pagination": {"offset": offset, "limit": limit, "total": len(records)},
                "monitors": records[offset : offset + limit],
            }

        if endpoint == "getPSPs":
            records = list(self.psps.values())
            return {
                "stat": "ok",
                "pagination": {"offset": offset, "limit": limit, "total": len(records)},
                "psps": records[offset : offset + limit],
            }

        if endpoint == "getAlertContacts":
            records = list(self.alert_contacts.values())
            return {
                "stat": "ok",
                "pagination": {"offset": offset, "limit": limit, "total": len(records)},
                "alert_contacts": records[offset : offset + limit],
            }

        if endpoint == "getMWindows":
            records = list(self.mwindows.values())
            return {
                "stat": "ok",
                "pagination": {"offset": offset, "limit": limit, "total": len(records)},
                "mwindows": records[offset : offset + limit],
            }

        raise UptimeRobotNotFoundError(f"Endpoint {endpoint} not recognized")
