import aiohttp
import asyncio
from typing import List, Dict, Any, Optional

class InfluxDBConnector:
    """
    InfluxDB v2 Time-Series Telemetry Connector for Cognee.
    
    Queries InfluxDB v2 via official Flux HTTP endpoint (/api/v2/query) and folds
    high-frequency time-series points into aggregated telemetry documents (min, max,
    mean, count, threshold breaches) ready for Cognee Graph RAG memory without graph bloating.
    """

    DEFAULT_URL = "http://localhost:8086"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        token: Optional[str] = None,
        org: Optional[str] = None,
        bucket: Optional[str] = None,
        measurements: Optional[List[str]] = None,
        range_days: int = 7,
        threshold_rules: Optional[Dict[str, float]] = None,
    ):
        """
        Initialize InfluxDBConnector.

        Args:
            url: Base URL of InfluxDB v2 instance (e.g., http://localhost:8086).
            token: InfluxDB v2 authentication token.
            org: Organization name or ID.
            bucket: Target bucket name containing time-series telemetry.
            measurements: Optional list of measurements to filter on.
            range_days: Historical range in days to query (default: 7).
            threshold_rules: Optional map of field -> upper threshold limit for breach detection.
        """
        if not token or not org or not bucket:
            raise ValueError("InfluxDB token, org, and bucket must be provided.")

        self.url = url.rstrip("/")
        self.token = token
        self.org = org
        self.bucket = bucket
        self.measurements = measurements or []
        self.range_days = max(1, min(range_days, 90))
        self.threshold_rules = threshold_rules or {}

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Token {self.token}",
            "Accept": "application/csv",
            "Content-Type": "application/vnd.flux",
            "User-Agent": "cognee-community-connector-influxdb/0.1.0",
        }

    def build_flux_query(self, measurement: Optional[str] = None) -> str:
        """Construct Flux query with range, measurement filter, and window aggregation."""
        meas_filter = f' |> filter(fn: (r) => r["_measurement"] == "{measurement}")' if measurement else ""
        query = f'''
from(bucket: "{self.bucket}")
  |> range(start: -{self.range_days}d)
  {meas_filter}
  |> filter(fn: (r) => r["_field"] != "")
'''
        return query.strip()

    def parse_flux_csv(self, csv_text: str) -> List[Dict[str, Any]]:
        """
        Parse annotated CSV returned by InfluxDB v2 Flux endpoint into row dictionaries.
        """
        rows = []
        lines = [line.strip() for line in csv_text.splitlines() if line.strip()]
        header = None

        for line in lines:
            if line.startswith("#"):
                continue  # Skip annotation lines
            parts = [p.strip() for p in line.split(",")]
            if header is None:
                header = parts
                continue
            
            if len(parts) >= len(header):
                row_dict = {}
                for idx, col in enumerate(header):
                    if col and idx < len(parts):
                        row_dict[col] = parts[idx]
                rows.append(row_dict)

        return rows

    def aggregate_points_by_series(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Fold high-frequency raw points into statistical summaries per series to prevent graph bloat.
        """
        series_map: Dict[str, Dict[str, Any]] = {}

        for row in rows:
            meas = row.get("_measurement", "unknown")
            field = row.get("_field", "value")
            val_str = row.get("_value", "")
            time_str = row.get("_time", "")

            try:
                val = float(val_str)
            except (ValueError, TypeError):
                continue

            series_key = f"{meas}:{field}"
            if series_key not in series_map:
                series_map[series_key] = {
                    "measurement": meas,
                    "field": field,
                    "values": [],
                    "first_time": time_str,
                    "last_time": time_str,
                }
            series_map[series_key]["values"].append(val)
            series_map[series_key]["last_time"] = time_str

        summaries = []
        for key, s in series_map.items():
            vals = s["values"]
            if not vals:
                continue

            count = len(vals)
            min_val = min(vals)
            max_val = max(vals)
            mean_val = sum(vals) / count
            last_val = vals[-1]

            # Detect threshold breaches
            breaches = []
            meas = s["measurement"]
            field = s["field"]
            if field in self.threshold_rules and max_val > self.threshold_rules[field]:
                breaches.append(f"Field '{field}' exceeded threshold limit of {self.threshold_rules[field]} (Peak: {max_val})")

            breach_text = "\nThreshold Alerts: " + "; ".join(breaches) if breaches else "\nThreshold Status: Normal (No breaches)"

            clean_text = f"""# InfluxDB Telemetry Summary: {meas}.{field}
Bucket: {self.bucket} | Org: {self.org}
Time Window: Last {self.range_days} days (from {s['first_time']} to {s['last_time']})
Total Data Points Aggregated: {count}

## Statistical Summary
- **Average (Mean)**: {mean_val:.2f}
- **Minimum**: {min_val:.2f}
- **Maximum**: {max_val:.2f}
- **Latest Reading**: {last_val:.2f}
{breach_text}
""".strip()

            summaries.append({
                "source_id": f"influxdb:{self.bucket}:{meas}:{field}",
                "measurement": meas,
                "field": field,
                "count": count,
                "min": min_val,
                "max": max_val,
                "mean": mean_val,
                "last": last_val,
                "breaches": breaches,
                "clean_text": clean_text,
            })

        return summaries

    async def fetch_telemetry(self, session: aiohttp.ClientSession, measurement: Optional[str] = None) -> List[Dict[str, Any]]:
        """Execute Flux query and return folded telemetry summaries."""
        endpoint = f"{self.url}/api/v2/query?org={self.org}"
        flux = self.build_flux_query(measurement)

        try:
            async with session.post(endpoint, data=flux, headers=self._headers()) as resp:
                if resp.status != 200:
                    return []
                csv_text = await resp.text()
                rows = self.parse_flux_csv(csv_text)
                return self.aggregate_points_by_series(rows)
        except Exception:
            return []

    async def fetch_all(self) -> List[Dict[str, Any]]:
        """Fetch all configured measurements or general bucket telemetry."""
        async with aiohttp.ClientSession() as session:
            if not self.measurements:
                return await self.fetch_telemetry(session, None)

            tasks = [self.fetch_telemetry(session, m) for m in self.measurements]
            results = await asyncio.gather(*tasks)

            all_docs = []
            for res in results:
                all_docs.extend(res)
            return all_docs
