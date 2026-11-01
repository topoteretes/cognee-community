import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp


@dataclass
class LemlistMetrics:
    emails_sent: int = 0
    emails_opened: int = 0
    emails_clicked: int = 0
    emails_replied: int = 0
    emails_bounced: int = 0
    unsubscribed: int = 0

    @property
    def open_rate(self) -> float:
        return (self.emails_opened / self.emails_sent * 100.0) if self.emails_sent > 0 else 0.0

    @property
    def reply_rate(self) -> float:
        return (self.emails_replied / self.emails_sent * 100.0) if self.emails_sent > 0 else 0.0

    @property
    def click_rate(self) -> float:
        return (self.emails_clicked / self.emails_sent * 100.0) if self.emails_sent > 0 else 0.0

    def to_markdown(self) -> str:
        lines = [
            "- **Total Emails Dispatched:** {:,}".format(self.emails_sent),
            "- **Unique Opens:** {:,} ({:.1f}%)".format(self.emails_opened, self.open_rate),
            "- **Direct Replies:** {:,} ({:.1f}%)".format(self.emails_replied, self.reply_rate),
            "- **Link Clicks:** {:,} ({:.1f}%)".format(self.emails_clicked, self.click_rate),
            "- **Bounces:** {:,}".format(self.emails_bounced),
            "- **Unsubscribes:** {:,}".format(self.unsubscribed),
        ]
        return "\n".join(lines)


@dataclass
class LemlistCampaign:
    id: str
    name: str
    status: str
    created_at: str
    updated_at: str
    timezone: str = "UTC"
    labels: List[str] = field(default_factory=list)
    metrics: Optional[LemlistMetrics] = None

    def to_markdown(self) -> str:
        lines = [
            f"# Outreach Campaign: {self.name}",
            f"- **Campaign ID:** {self.id}",
            f"- **Status:** {self.status.upper()}",
            f"- **Timezone:** {self.timezone}",
            f"- **Created At:** {self.created_at}",
            f"- **Last Updated:** {self.updated_at}",
        ]
        if self.labels:
            lines.append(f"- **Labels:** {', '.join(self.labels)}")
        if self.metrics:
            lines.append("\n### Campaign Engagement Performance:")
            lines.append(self.metrics.to_markdown())
        return "\n".join(lines)


class LemlistConnector:
    """Cognee community connector for lemlist sales outreach & campaign intelligence REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.lemlist.com/api",
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def _get_auth_header(self) -> Dict[str, str]:
        # lemlist uses HTTP Basic Auth with empty username and API key as password
        token_bytes = f":{self.api_key}".encode("utf-8")
        encoded = base64.b64encode(token_bytes).decode("ascii")
        return {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "User-Agent": "Cognee-Connector-Lemlist/0.1.0",
        }

    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        headers = self._get_auth_header()

        async with session.get(url, headers=headers, params=params or {}) as response:
            if response.status == 401:
                raise PermissionError("lemlist authentication failed: Invalid or expired API key")
            if response.status == 403:
                raise PermissionError("lemlist authorization failed: Insufficient permissions")
            if response.status == 429:
                raise RuntimeError("lemlist API rate limit exceeded (HTTP 429)")
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"lemlist API HTTP {response.status} error: {text}")

            return await response.json()

    async def fetch_campaign_stats(self, campaign_id: str) -> Optional[LemlistMetrics]:
        """Fetch aggregated engagement statistics for a campaign."""
        try:
            data = await self._get(f"/campaigns/{campaign_id}/stats")
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        return LemlistMetrics(
            emails_sent=int(data.get("emailsSent", 0) or 0),
            emails_opened=int(data.get("emailsOpened", 0) or 0),
            emails_clicked=int(data.get("emailsClicked", 0) or 0),
            emails_replied=int(data.get("emailsReplied", 0) or 0),
            emails_bounced=int(data.get("emailsBounced", 0) or 0),
            unsubscribed=int(data.get("unsubscribed", 0) or 0),
        )

    async def fetch_campaigns(
        self,
        limit: int = 50,
        include_stats: bool = True,
    ) -> List[LemlistCampaign]:
        """Fetch active, paused, and archived sales campaigns."""
        data = await self._get("/campaigns", params={"limit": limit})
        campaigns = []

        if isinstance(data, list):
            for it in data[:limit]:
                c_id = str(it.get("_id") or it.get("id", ""))
                # Look for embedded stats or fetch separately
                stats_info = it.get("stats")
                metrics = None
                if isinstance(stats_info, dict):
                    metrics = LemlistMetrics(
                        emails_sent=int(stats_info.get("emailsSent", 0) or 0),
                        emails_opened=int(stats_info.get("emailsOpened", 0) or 0),
                        emails_clicked=int(stats_info.get("emailsClicked", 0) or 0),
                        emails_replied=int(stats_info.get("emailsReplied", 0) or 0),
                        emails_bounced=int(stats_info.get("emailsBounced", 0) or 0),
                        unsubscribed=int(stats_info.get("unsubscribed", 0) or 0),
                    )
                elif include_stats and c_id:
                    metrics = await self.fetch_campaign_stats(c_id)

                camp = LemlistCampaign(
                    id=c_id,
                    name=it.get("name", f"Campaign #{c_id}"),
                    status=it.get("status", "draft"),
                    created_at=it.get("createdAt", ""),
                    updated_at=it.get("updatedAt", ""),
                    timezone=it.get("timezone", "UTC"),
                    labels=it.get("labels", []),
                    metrics=metrics,
                )
                campaigns.append(camp)

        return campaigns

    async def to_documents(
        self,
        limit: int = 50,
        include_stats: bool = True,
    ) -> List[str]:
        """Convert all outreach campaigns into Markdown documents for Cognee memory ingestion."""
        campaigns = await self.fetch_campaigns(limit=limit, include_stats=include_stats)
        return [c.to_markdown() for c in campaigns]
