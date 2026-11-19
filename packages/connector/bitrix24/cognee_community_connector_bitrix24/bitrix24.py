import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp


@dataclass
class Bitrix24Comment:
    id: str
    entity_id: str
    entity_type: str
    author_id: str
    comment: str
    created_at: str

    def to_markdown(self) -> str:
        clean_comment = re.sub(r"<[^>]+>", "", self.comment).strip()
        return f"- [{self.created_at}] (Author {self.author_id}): {clean_comment}"


@dataclass
class Bitrix24Deal:
    id: str
    title: str
    stage_id: str
    opportunity: float
    currency_id: str
    company_id: Optional[str] = None
    contact_id: Optional[str] = None
    date_create: str = ""
    date_modify: str = ""
    begin_date: str = ""
    close_date: str = ""
    comments: List[Bitrix24Comment] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# CRM Deal: {self.title}",
            f"- **Deal ID:** {self.id}",
            f"- **Stage:** {self.stage_id}",
            f"- **Amount:** {self.opportunity:.2f} {self.currency_id}",
            f"- **Company ID:** {self.company_id or 'None'}",
            f"- **Contact ID:** {self.contact_id or 'None'}",
            f"- **Created:** {self.date_create}",
            f"- **Modified:** {self.date_modify}",
            f"- **Close Date:** {self.close_date or 'Open'}",
        ]
        if self.comments:
            lines.append("\n### Timeline Comments:")
            for comment in self.comments:
                lines.append(comment.to_markdown())
        return "\n".join(lines)


@dataclass
class Bitrix24Contact:
    id: str
    name: str
    last_name: str
    post: str = ""
    phone: str = ""
    email: str = ""
    comments_text: str = ""
    date_create: str = ""
    date_modify: str = ""
    comments: List[Bitrix24Comment] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.name} {self.last_name}".strip() or f"Contact #{self.id}"

    def to_markdown(self) -> str:
        lines = [
            f"# CRM Contact: {self.full_name}",
            f"- **Contact ID:** {self.id}",
            f"- **Job Title:** {self.post or 'Unspecified'}",
            f"- **Phone:** {self.phone or 'N/A'}",
            f"- **Email:** {self.email or 'N/A'}",
            f"- **Created:** {self.date_create}",
            f"- **Modified:** {self.date_modify}",
        ]
        if self.comments_text:
            clean_text = re.sub(r"<[^>]+>", "", self.comments_text).strip()
            lines.append(f"\n**Profile Notes:** {clean_text}")
        if self.comments:
            lines.append("\n### Timeline Comments:")
            for comment in self.comments:
                lines.append(comment.to_markdown())
        return "\n".join(lines)


@dataclass
class Bitrix24Company:
    id: str
    title: str
    company_type: str = ""
    industry: str = ""
    revenue: float = 0.0
    currency_id: str = "USD"
    phone: str = ""
    email: str = ""
    comments_text: str = ""
    date_create: str = ""
    date_modify: str = ""
    comments: List[Bitrix24Comment] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# CRM Company: {self.title}",
            f"- **Company ID:** {self.id}",
            f"- **Type:** {self.company_type or 'General'}",
            f"- **Industry:** {self.industry or 'Unspecified'}",
            f"- **Annual Revenue:** {self.revenue:.2f} {self.currency_id}",
            f"- **Phone:** {self.phone or 'N/A'}",
            f"- **Email:** {self.email or 'N/A'}",
            f"- **Created:** {self.date_create}",
            f"- **Modified:** {self.date_modify}",
        ]
        if self.comments_text:
            clean_text = re.sub(r"<[^>]+>", "", self.comments_text).strip()
            lines.append(f"\n**Company Description:** {clean_text}")
        if self.comments:
            lines.append("\n### Timeline Comments:")
            for comment in self.comments:
                lines.append(comment.to_markdown())
        return "\n".join(lines)


class Bitrix24Connector:
    """Cognee connector for Bitrix24 CRM inbound webhook API."""

    def __init__(
        self,
        webhook_url: str,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.webhook_url = webhook_url.rstrip("/")
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

    async def _post(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        session = await self._get_session()
        url = f"{self.webhook_url}/{method}.json"
        async with session.post(url, json=payload or {}) as response:
            if response.status == 401:
                data = await response.json()
                raise PermissionError(f"Bitrix24 authentication failed: {data.get('error', 'INVALID_CREDENTIALS')}")
            if response.status == 429:
                raise RuntimeError("Bitrix24 operation time limit exceeded (HTTP 429)")
            if response.status == 503:
                raise RuntimeError("Bitrix24 query rate limit exceeded (HTTP 503)")
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"Bitrix24 API error ({response.status}): {text}")
            return await response.json()

    async def fetch_comments(self, entity_type: str, entity_id: str) -> List[Bitrix24Comment]:
        """Fetch timeline comments for a specific CRM entity record."""
        payload = {
            "filter": {
                "ENTITY_ID": entity_id,
                "ENTITY_TYPE": entity_type.lower(),
            },
            "order": {"CREATED": "ASC"},
        }
        try:
            data = await self._post("crm.timeline.comment.list", payload)
        except Exception:
            return []

        results = data.get("result", [])
        comments = []
        for item in results:
            comments.append(
                Bitrix24Comment(
                    id=str(item.get("ID", "")),
                    entity_id=str(item.get("ENTITY_ID", entity_id)),
                    entity_type=str(item.get("ENTITY_TYPE", entity_type)),
                    author_id=str(item.get("AUTHOR_ID", "")),
                    comment=item.get("COMMENT", ""),
                    created_at=item.get("CREATED", ""),
                )
            )
        return comments

    async def fetch_deals(
        self,
        stage_id: Optional[str] = None,
        limit: int = 50,
        include_comments: bool = True,
    ) -> List[Bitrix24Deal]:
        """Fetch CRM deals and optionally attach timeline comments."""
        filters = {}
        if stage_id:
            filters["STAGE_ID"] = stage_id

        payload: Dict[str, Any] = {
            "order": {"DATE_MODIFY": "DESC"},
            "filter": filters,
            "select": [
                "ID",
                "TITLE",
                "STAGE_ID",
                "OPPORTUNITY",
                "CURRENCY_ID",
                "COMPANY_ID",
                "CONTACT_ID",
                "DATE_CREATE",
                "DATE_MODIFY",
                "BEGINDATE",
                "CLOSEDATE",
            ],
        }

        data = await self._post("crm.deal.list", payload)
        results = data.get("result", [])
        deals = []

        for item in results[:limit]:
            deal_id = str(item.get("ID", ""))
            deal = Bitrix24Deal(
                id=deal_id,
                title=item.get("TITLE", f"Deal #{deal_id}"),
                stage_id=item.get("STAGE_ID", "NEW"),
                opportunity=float(item.get("OPPORTUNITY", 0.0) or 0.0),
                currency_id=item.get("CURRENCY_ID", "USD"),
                company_id=str(item["COMPANY_ID"]) if item.get("COMPANY_ID") else None,
                contact_id=str(item["CONTACT_ID"]) if item.get("CONTACT_ID") else None,
                date_create=item.get("DATE_CREATE", ""),
                date_modify=item.get("DATE_MODIFY", ""),
                begin_date=item.get("BEGINDATE", ""),
                close_date=item.get("CLOSEDATE", ""),
            )
            if include_comments:
                deal.comments = await self.fetch_comments("deal", deal_id)
            deals.append(deal)

        return deals

    async def fetch_contacts(
        self,
        limit: int = 50,
        include_comments: bool = True,
    ) -> List[Bitrix24Contact]:
        """Fetch CRM contacts with contact channels and timeline comments."""
        payload: Dict[str, Any] = {
            "order": {"DATE_MODIFY": "DESC"},
            "select": ["ID", "NAME", "LAST_NAME", "POST", "COMMENTS", "DATE_CREATE", "DATE_MODIFY", "PHONE", "EMAIL"],
        }
        data = await self._post("crm.contact.list", payload)
        results = data.get("result", [])
        contacts = []

        for item in results[:limit]:
            cid = str(item.get("ID", ""))
            phone_val = ""
            if isinstance(item.get("PHONE"), list) and item["PHONE"]:
                phone_val = item["PHONE"][0].get("VALUE", "")
            elif isinstance(item.get("PHONE"), str):
                phone_val = item["PHONE"]

            email_val = ""
            if isinstance(item.get("EMAIL"), list) and item["EMAIL"]:
                email_val = item["EMAIL"][0].get("VALUE", "")
            elif isinstance(item.get("EMAIL"), str):
                email_val = item["EMAIL"]

            contact = Bitrix24Contact(
                id=cid,
                name=item.get("NAME", ""),
                last_name=item.get("LAST_NAME", ""),
                post=item.get("POST", ""),
                phone=phone_val,
                email=email_val,
                comments_text=item.get("COMMENTS", ""),
                date_create=item.get("DATE_CREATE", ""),
                date_modify=item.get("DATE_MODIFY", ""),
            )
            if include_comments:
                contact.comments = await self.fetch_comments("contact", cid)
            contacts.append(contact)

        return contacts

    async def fetch_companies(
        self,
        limit: int = 50,
        include_comments: bool = True,
    ) -> List[Bitrix24Company]:
        """Fetch CRM companies with metadata and timeline comments."""
        payload: Dict[str, Any] = {
            "order": {"DATE_MODIFY": "DESC"},
            "select": [
                "ID",
                "TITLE",
                "COMPANY_TYPE",
                "INDUSTRY",
                "REVENUE",
                "CURRENCY_ID",
                "PHONE",
                "EMAIL",
                "COMMENTS",
                "DATE_CREATE",
                "DATE_MODIFY",
            ],
        }
        data = await self._post("crm.company.list", payload)
        results = data.get("result", [])
        companies = []

        for item in results[:limit]:
            comp_id = str(item.get("ID", ""))
            phone_val = ""
            if isinstance(item.get("PHONE"), list) and item["PHONE"]:
                phone_val = item["PHONE"][0].get("VALUE", "")

            email_val = ""
            if isinstance(item.get("EMAIL"), list) and item["EMAIL"]:
                email_val = item["EMAIL"][0].get("VALUE", "")

            company = Bitrix24Company(
                id=comp_id,
                title=item.get("TITLE", f"Company #{comp_id}"),
                company_type=item.get("COMPANY_TYPE", "CUSTOMER"),
                industry=item.get("INDUSTRY", "IT"),
                revenue=float(item.get("REVENUE", 0.0) or 0.0),
                currency_id=item.get("CURRENCY_ID", "USD"),
                phone=phone_val,
                email=email_val,
                comments_text=item.get("COMMENTS", ""),
                date_create=item.get("DATE_CREATE", ""),
                date_modify=item.get("DATE_MODIFY", ""),
            )
            if include_comments:
                company.comments = await self.fetch_comments("company", comp_id)
            companies.append(company)

        return companies

    async def fetch_all_crm_data(
        self,
        crm_types: Optional[List[str]] = None,
        limit_per_type: int = 50,
        include_comments: bool = True,
    ) -> Dict[str, List[Any]]:
        """Fetch all requested CRM entities."""
        crm_types = crm_types or ["deal", "contact", "company"]
        output: Dict[str, List[Any]] = {
            "deals": [],
            "contacts": [],
            "companies": [],
        }

        if "deal" in crm_types:
            output["deals"] = await self.fetch_deals(limit=limit_per_type, include_comments=include_comments)
        if "contact" in crm_types:
            output["contacts"] = await self.fetch_contacts(limit=limit_per_type, include_comments=include_comments)
        if "company" in crm_types:
            output["companies"] = await self.fetch_companies(limit=limit_per_type, include_comments=include_comments)

        return output

    async def to_documents(
        self,
        crm_types: Optional[List[str]] = None,
        limit_per_type: int = 50,
        include_comments: bool = True,
    ) -> List[str]:
        """Convert CRM records to Markdown document strings for Cognee cognition."""
        data = await self.fetch_all_crm_data(
            crm_types=crm_types,
            limit_per_type=limit_per_type,
            include_comments=include_comments,
        )
        docs = []
        for deal in data["deals"]:
            docs.append(deal.to_markdown())
        for contact in data["contacts"]:
            docs.append(contact.to_markdown())
        for company in data["companies"]:
            docs.append(company.to_markdown())
        return docs
