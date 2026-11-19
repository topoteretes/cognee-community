import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp


@dataclass
class WooCommerceReview:
    id: str
    product_id: str
    reviewer: str
    review: str
    rating: int
    date_created: str

    def to_markdown(self) -> str:
        clean_text = re.sub(r"<[^>]+>", "", self.review).strip()
        clean_text = re.sub(r"\s+", " ", clean_text)
        return f"- [{self.date_created}] {self.reviewer} ({self.rating}/5 stars): {clean_text}"


@dataclass
class WooCommerceProduct:
    id: str
    name: str
    slug: str
    permalink: str
    status: str
    price: float
    regular_price: float
    sale_price: Optional[float] = None
    sku: str = ""
    stock_status: str = "instock"
    categories: List[str] = field(default_factory=list)
    short_description: str = ""
    description: str = ""
    reviews: List[WooCommerceReview] = field(default_factory=list)

    def to_markdown(self) -> str:
        clean_desc = re.sub(r"<[^>]+>", "", self.description).strip()
        clean_desc = re.sub(r"\s+", " ", clean_desc)
        clean_short = re.sub(r"<[^>]+>", "", self.short_description).strip()

        lines = [
            f"# Product: {self.name}",
            f"- **Product ID:** {self.id}",
            f"- **SKU:** {self.sku or 'N/A'}",
            f"- **Status:** {self.status} (Stock: {self.stock_status})",
            f"- **Price:** ${self.price:.2f}",
        ]
        if self.regular_price and self.sale_price:
            lines.append(f"- **Regular Price:** ${self.regular_price:.2f} (Sale: ${self.sale_price:.2f})")
        if self.categories:
            lines.append(f"- **Categories:** {', '.join(self.categories)}")
        if self.permalink:
            lines.append(f"- **Product URL:** {self.permalink}")
        if clean_short:
            lines.append(f"\n### Summary:\n{clean_short}")
        if clean_desc:
            lines.append(f"\n### Description:\n{clean_desc}")
        if self.reviews:
            lines.append("\n### Verified Customer Reviews:")
            for r in self.reviews:
                lines.append(r.to_markdown())
        return "\n".join(lines)


@dataclass
class WooCommerceOrderNote:
    id: str
    author: str
    date_created: str
    note: str
    customer_note: bool = False

    def to_markdown(self) -> str:
        tag = " [Customer Visible]" if self.customer_note else " [Internal Staff Note]"
        return f"- [{self.date_created}] ({self.author}){tag}: {self.note.strip()}"


@dataclass
class WooCommerceOrder:
    id: str
    number: str
    status: str
    currency: str
    date_created: str
    total: float
    customer_id: str
    line_items: List[Dict[str, Any]] = field(default_factory=list)
    customer_note: str = ""
    order_notes: List[WooCommerceOrderNote] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Order #{self.number}",
            f"- **Order ID:** {self.id}",
            f"- **Status:** {self.status}",
            f"- **Date Created:** {self.date_created}",
            f"- **Total Amount:** {self.total:.2f} {self.currency}",
            f"- **Customer ID:** {self.customer_id}",
        ]
        if self.customer_note:
            lines.append(f"\n### Customer Note at Checkout:\n{self.customer_note.strip()}")
        if self.line_items:
            lines.append("\n### Purchased Items:")
            for item in self.line_items:
                name = item.get("name", "Unknown item")
                qty = item.get("quantity", 1)
                subtotal = item.get("total", "0.00")
                lines.append(f"- {qty}x {name} (Subtotal: {subtotal} {self.currency})")
        if self.order_notes:
            lines.append("\n### Order Notes Timeline:")
            for note in self.order_notes:
                lines.append(note.to_markdown())
        return "\n".join(lines)


class WooCommerceConnector:
    """Cognee community connector for WooCommerce REST API v3."""

    def __init__(
        self,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        include_customer_pii: bool = False,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.store_url = store_url.rstrip("/")
        self.consumer_key = consumer_key.strip()
        self.consumer_secret = consumer_secret.strip()
        self.include_customer_pii = include_customer_pii
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
        url = f"{self.store_url}/wp-json/wc/v3{endpoint}"

        auth = aiohttp.BasicAuth(self.consumer_key, self.consumer_secret)
        headers = {
            "Accept": "application/json",
            "User-Agent": "Cognee-Connector-WooCommerce/0.1.0",
        }

        async with session.get(url, auth=auth, headers=headers, params=params or {}) as response:
            if response.status == 401:
                raise PermissionError("WooCommerce authentication failed: Invalid Consumer Key or Secret")
            if response.status == 403:
                raise PermissionError("WooCommerce authorization failed: Insufficient permissions (read required)")
            if response.status == 429:
                raise RuntimeError("WooCommerce API rate limit exceeded (HTTP 429)")
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"WooCommerce API HTTP {response.status} error: {text}")

            data = await response.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return [data]
            return []

    async def fetch_product_reviews(self, product_id: Optional[str] = None, limit: int = 50) -> List[WooCommerceReview]:
        """Fetch verified customer reviews for products."""
        params: Dict[str, Any] = {"per_page": limit, "status": "approved"}
        if product_id:
            params["product"] = [int(product_id)]

        items = await self._get("/products/reviews", params=params)
        reviews = []
        for it in items:
            reviews.append(
                WooCommerceReview(
                    id=str(it.get("id", "")),
                    product_id=str(it.get("product_id", "")),
                    reviewer=it.get("reviewer", "Anonymous Reviewer"),
                    review=it.get("review", ""),
                    rating=int(it.get("rating", 5) or 5),
                    date_created=it.get("date_created", ""),
                )
            )
        return reviews

    async def fetch_products(
        self,
        limit: int = 50,
        include_reviews: bool = True,
    ) -> List[WooCommerceProduct]:
        """Fetch store product catalog with inventory, pricing, and reviews."""
        items = await self._get(
            "/products",
            params={"per_page": limit, "status": "publish"},
        )
        products = []
        for it in items:
            cats = [c.get("name", "") for c in it.get("categories", []) if c.get("name")]
            p_id = str(it.get("id", ""))
            price_val = float(it.get("price", 0.0) or 0.0)
            reg_price = float(it.get("regular_price", 0.0) or 0.0)
            sale_price = float(it.get("sale_price", 0.0)) if it.get("sale_price") else None

            prod = WooCommerceProduct(
                id=p_id,
                name=it.get("name", f"Product #{p_id}"),
                slug=it.get("slug", ""),
                permalink=it.get("permalink", ""),
                status=it.get("status", "publish"),
                price=price_val,
                regular_price=reg_price,
                sale_price=sale_price,
                sku=it.get("sku", ""),
                stock_status=it.get("stock_status", "instock"),
                categories=cats,
                short_description=it.get("short_description", ""),
                description=it.get("description", ""),
            )
            if include_reviews:
                prod.reviews = await self.fetch_product_reviews(product_id=p_id, limit=10)
            products.append(prod)

        return products

    async def fetch_order_notes(self, order_id: str) -> List[WooCommerceOrderNote]:
        """Fetch internal and customer-facing notes for an order."""
        try:
            items = await self._get(f"/orders/{order_id}/notes")
        except Exception:
            return []

        notes = []
        for it in items:
            notes.append(
                WooCommerceOrderNote(
                    id=str(it.get("id", "")),
                    author=it.get("author", "Staff"),
                    date_created=it.get("date_created", ""),
                    note=it.get("note", ""),
                    customer_note=bool(it.get("customer_note", False)),
                )
            )
        return notes

    async def fetch_orders(
        self,
        limit: int = 50,
        include_notes: bool = True,
    ) -> List[WooCommerceOrder]:
        """Fetch store orders, items, and timeline notes."""
        items = await self._get(
            "/orders",
            params={"per_page": limit},
        )
        orders = []
        for it in items:
            o_id = str(it.get("id", ""))
            total_val = float(it.get("total", 0.0) or 0.0)

            clean_line_items = []
            for li in it.get("line_items", []):
                clean_line_items.append(
                    {
                        "id": li.get("id"),
                        "name": li.get("name"),
                        "product_id": li.get("product_id"),
                        "quantity": li.get("quantity", 1),
                        "total": li.get("total", "0.00"),
                    }
                )

            order = WooCommerceOrder(
                id=o_id,
                number=str(it.get("number", o_id)),
                status=it.get("status", "processing"),
                currency=it.get("currency", "USD"),
                date_created=it.get("date_created", ""),
                total=total_val,
                customer_id=str(it.get("customer_id", "0")),
                line_items=clean_line_items,
                customer_note=it.get("customer_note", ""),
            )
            if include_notes:
                order.order_notes = await self.fetch_order_notes(order_id=o_id)
            orders.append(order)

        return orders

    async def to_documents(
        self,
        include_products: bool = True,
        include_orders: bool = True,
        limit_per_type: int = 50,
    ) -> List[str]:
        """Convert WooCommerce store data into Cognee Markdown documents."""
        docs = []
        if include_products:
            prods = await self.fetch_products(limit=limit_per_type, include_reviews=True)
            for p in prods:
                docs.append(p.to_markdown())

        if include_orders:
            orders = await self.fetch_orders(limit=limit_per_type, include_notes=True)
            for o in orders:
                docs.append(o.to_markdown())

        return docs
