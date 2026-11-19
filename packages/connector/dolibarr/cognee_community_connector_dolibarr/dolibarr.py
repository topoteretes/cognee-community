import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp


@dataclass
class DolibarrContact:
    id: str
    firstname: str
    lastname: str
    socid: Optional[str] = None
    phone: str = ""
    email: str = ""
    post: str = ""
    note_public: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.firstname} {self.lastname}".strip() or f"Contact #{self.id}"

    def to_markdown(self) -> str:
        lines = [
            f"# CRM Contact: {self.full_name}",
            f"- **Contact ID:** {self.id}",
            f"- **Company (SocID):** {self.socid or 'None'}",
            f"- **Job Title:** {self.post or 'Unspecified'}",
            f"- **Phone:** {self.phone or 'N/A'}",
            f"- **Email:** {self.email or 'N/A'}",
        ]
        if self.note_public:
            clean_note = re.sub(r"<[^>]+>", "", self.note_public).strip()
            lines.append(f"\n**Public Note:** {clean_note}")
        return "\n".join(lines)


@dataclass
class DolibarrProposal:
    id: str
    ref: str
    socid: str
    total_ht: float
    total_ttc: float
    status_label: str
    date_validation: str = ""

    def to_markdown(self) -> str:
        return "\n".join(
            [
                f"# Commercial Proposal: {self.ref}",
                f"- **Proposal ID:** {self.id}",
                f"- **Third-Party (SocID):** {self.socid}",
                f"- **Status:** {self.status_label}",
                f"- **Amount (Excl. Tax):** {self.total_ht:.2f}",
                f"- **Total Amount (Incl. Tax):** {self.total_ttc:.2f}",
                f"- **Validation Date:** {self.date_validation or 'Pending'}",
            ]
        )


@dataclass
class DolibarrTicket:
    id: str
    ref: str
    track_id: str
    subject: str
    message: str
    type_label: str = ""
    category_label: str = ""
    severity_label: str = ""
    status_label: str = ""

    def to_markdown(self) -> str:
        clean_msg = re.sub(r"<[^>]+>", "", self.message).strip()
        lines = [
            f"# Support Ticket: [{self.ref}] {self.subject}",
            f"- **Ticket ID:** {self.id}",
            f"- **Tracking ID:** {self.track_id}",
            f"- **Type:** {self.type_label or 'Support'}",
            f"- **Category:** {self.category_label or 'General'}",
            f"- **Severity:** {self.severity_label or 'Normal'}",
            f"- **Status:** {self.status_label or 'Open'}",
            f"\n### Ticket Initial Message:\n{clean_msg}",
        ]
        return "\n".join(lines)


@dataclass
class DolibarrThirdParty:
    id: str
    name: str
    name_alias: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    town: str = ""
    zip_code: str = ""
    status: str = "Active"
    last_modified: str = ""
    note_public: str = ""
    note_private: str = ""
    contacts: List[DolibarrContact] = field(default_factory=list)
    proposals: List[DolibarrProposal] = field(default_factory=list)
    tickets: List[DolibarrTicket] = field(default_factory=list)

    def to_markdown(self, include_private_notes: bool = False) -> str:
        location = ", ".join(filter(None, [self.address, self.town, self.zip_code]))
        lines = [
            f"# Third-Party (Company/Client): {self.name}",
            f"- **ID:** {self.id}",
            f"- **Status:** {self.status}",
            f"- **Phone:** {self.phone or 'N/A'}",
            f"- **Email:** {self.email or 'N/A'}",
            f"- **Location:** {location or 'N/A'}",
            f"- **Last Modified:** {self.last_modified}",
        ]
        if self.note_public:
            clean_pub = re.sub(r"<[^>]+>", "", self.note_public).strip()
            lines.append(f"\n### Public Profile Notes:\n{clean_pub}")
        if include_private_notes and self.note_private:
            clean_priv = re.sub(r"<[^>]+>", "", self.note_private).strip()
            lines.append(f"\n### Private Internal Notes [RESTRICTED]:\n{clean_priv}")
        return "\n".join(lines)


class DolibarrConnector:
    """Cognee community connector for Dolibarr open-source ERP/CRM REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        include_private_notes: bool = False,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.include_private_notes = include_private_notes
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

    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        session = await self._get_session()
        url = f"{self.base_url}/api/index.php{endpoint}"
        headers = {
            "DOLAPIKEY": self.api_key,
            "Accept": "application/json",
            "User-Agent": "Cognee-Connector-Dolibarr/0.1.0",
        }

        async with session.get(url, headers=headers, params=params or {}) as response:
            if response.status == 401:
                raise PermissionError("Dolibarr authentication failed: Invalid or expired DOLAPIKEY")
            if response.status == 404:
                # Dolibarr quirk: returns 404 when 0 records match the query
                try:
                    data = await response.json()
                    err_msg = ""
                    if isinstance(data, dict):
                        err_msg = str(data.get("error", {}).get("message", "")).lower()
                    if "not found" in err_msg or "no " in err_msg:
                        return []
                except Exception:
                    pass
                return []
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"Dolibarr API HTTP {response.status} error: {text}")

            result = await response.json()
            if isinstance(result, list):
                return result
            elif isinstance(result, dict):
                return [result]
            return []

    async def fetch_thirdparties(self, limit: int = 50) -> List[DolibarrThirdParty]:
        """Fetch third-party clients and suppliers."""
        items = await self._get(
            "/thirdparties",
            params={"sortfield": "t.tms", "sortorder": "DESC", "limit": limit},
        )
        parties = []
        for it in items:
            parties.append(
                DolibarrThirdParty(
                    id=str(it.get("id", "")),
                    name=it.get("name", f"ThirdParty #{it.get('id', '')}"),
                    name_alias=it.get("name_alias", ""),
                    email=it.get("email", ""),
                    phone=it.get("phone", ""),
                    address=it.get("address", ""),
                    town=it.get("town", ""),
                    zip_code=it.get("zip", ""),
                    status="Active" if str(it.get("status", "1")) == "1" else "Closed",
                    last_modified=str(it.get("tms", "")),
                    note_public=it.get("note_public", ""),
                    note_private=it.get("note_private", ""),
                )
            )
        return parties

    async def fetch_contacts(self, limit: int = 50) -> List[DolibarrContact]:
        """Fetch individual contacts associated with third parties."""
        items = await self._get(
            "/contacts",
            params={"sortfield": "t.tms", "sortorder": "DESC", "limit": limit},
        )
        contacts = []
        for it in items:
            contacts.append(
                DolibarrContact(
                    id=str(it.get("id", "")),
                    firstname=it.get("firstname", ""),
                    lastname=it.get("lastname", ""),
                    socid=str(it["socid"]) if it.get("socid") else None,
                    phone=it.get("phone", ""),
                    email=it.get("email", ""),
                    post=it.get("post", ""),
                    note_public=it.get("note_public", ""),
                )
            )
        return contacts

    async def fetch_proposals(self, limit: int = 50) -> List[DolibarrProposal]:
        """Fetch commercial proposals and quote totals."""
        items = await self._get(
            "/proposals",
            params={"sortfield": "t.tms", "sortorder": "DESC", "limit": limit},
        )
        status_map = {
            "0": "Draft",
            "1": "Open / Validated",
            "2": "Signed",
            "3": "Not Signed",
            "4": "Billed",
        }
        proposals = []
        for it in items:
            raw_st = str(it.get("statut", "0"))
            proposals.append(
                DolibarrProposal(
                    id=str(it.get("id", "")),
                    ref=it.get("ref", f"PROP-{it.get('id', '')}"),
                    socid=str(it.get("socid", "")),
                    total_ht=float(it.get("total_ht", 0.0) or 0.0),
                    total_ttc=float(it.get("total_ttc", 0.0) or 0.0),
                    status_label=status_map.get(raw_st, f"Status #{raw_st}"),
                    date_validation=str(it.get("date_validation", "")),
                )
            )
        return proposals

    async def fetch_tickets(self, limit: int = 50) -> List[DolibarrTicket]:
        """Fetch customer support tickets."""
        items = await self._get(
            "/tickets",
            params={"sortfield": "t.tms", "sortorder": "DESC", "limit": limit},
        )
        tickets = []
        for it in items:
            tickets.append(
                DolibarrTicket(
                    id=str(it.get("id", "")),
                    ref=it.get("ref", f"TICK-{it.get('id', '')}"),
                    track_id=it.get("track_id", ""),
                    subject=it.get("subject", "Support Inquiry"),
                    message=it.get("message", ""),
                    type_label=it.get("type_label", ""),
                    category_label=it.get("category_label", ""),
                    severity_label=it.get("severity_label", ""),
                    status_label=it.get("status_label", "Open"),
                )
            )
        return tickets

    async def to_documents(
        self,
        include_thirdparties: bool = True,
        include_contacts: bool = True,
        include_proposals: bool = True,
        include_tickets: bool = True,
        limit_per_type: int = 50,
    ) -> List[str]:
        """Convert all selected Dolibarr ERP/CRM data into Cognee Markdown documents."""
        docs = []
        if include_thirdparties:
            parties = await self.fetch_thirdparties(limit=limit_per_type)
            for p in parties:
                docs.append(p.to_markdown(include_private_notes=self.include_private_notes))

        if include_contacts:
            contacts = await self.fetch_contacts(limit=limit_per_type)
            for c in contacts:
                docs.append(c.to_markdown())

        if include_proposals:
            proposals = await self.fetch_proposals(limit=limit_per_type)
            for prop in proposals:
                docs.append(prop.to_markdown())

        if include_tickets:
            tickets = await self.fetch_tickets(limit=limit_per_type)
            for tick in tickets:
                docs.append(tick.to_markdown())

        return docs
