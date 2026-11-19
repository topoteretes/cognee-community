import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cognee_community_connector_woocommerce.woocommerce import (
    WooCommerceConnector,
    WooCommerceProduct,
    WooCommerceReview,
    WooCommerceOrder,
    WooCommerceOrderNote,
)


@pytest.fixture
def wc_config():
    return {
        "store_url": "https://store.example.com",
        "consumer_key": "ck_test1234567890",
        "consumer_secret": "cs_test0987654321",
    }


def test_fetch_products_with_reviews(wc_config):
    async def run_test():
        products_data = [
            {
                "id": 101,
                "name": "Organic Linen Casual Shirt",
                "slug": "organic-linen-shirt",
                "permalink": "https://store.example.com/product/organic-linen-shirt",
                "status": "publish",
                "price": "64.00",
                "regular_price": "80.00",
                "sale_price": "64.00",
                "sku": "SHIRT-LINEN-01",
                "stock_status": "instock",
                "categories": [{"id": 3, "name": "Apparel"}, {"id": 8, "name": "Summer Wear"}],
                "short_description": "<p>Breathable 100% certified organic linen.</p>",
                "description": "<p>Tailored fit crafted for warm climates and relaxed elegance.</p>",
            }
        ]

        reviews_data = [
            {
                "id": 12,
                "product_id": 101,
                "reviewer": "Marcus Vance",
                "review": "<p>Outstanding texture and quality. Fits true to size.</p>",
                "rating": 5,
                "date_created": "2026-10-18T10:00:00",
            }
        ]

        mock_session = MagicMock()

        def mock_get(url, auth=None, headers=None, params=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "/products/reviews" in url:
                mock_resp.json = AsyncMock(return_value=reviews_data)
            elif "/products" in url:
                mock_resp.json = AsyncMock(return_value=products_data)
            else:
                mock_resp.json = AsyncMock(return_value=[])

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = WooCommerceConnector(
            store_url=wc_config["store_url"],
            consumer_key=wc_config["consumer_key"],
            consumer_secret=wc_config["consumer_secret"],
            session=mock_session,
        )

        products = await connector.fetch_products(include_reviews=True)
        assert len(products) == 1
        p = products[0]
        assert p.id == "101"
        assert p.name == "Organic Linen Casual Shirt"
        assert p.price == 64.0
        assert p.regular_price == 80.0
        assert p.categories == ["Apparel", "Summer Wear"]
        assert len(p.reviews) == 1
        assert p.reviews[0].reviewer == "Marcus Vance"

        md = p.to_markdown()
        assert "# Product: Organic Linen Casual Shirt" in md
        assert "**SKU:** SHIRT-LINEN-01" in md
        assert "**Price:** $64.00" in md
        assert "**Regular Price:** $80.00 (Sale: $64.00)" in md
        assert "Verified Customer Reviews:" in md
        assert "Marcus Vance (5/5 stars)" in md

    asyncio.run(run_test())


def test_fetch_orders_with_notes(wc_config):
    async def run_test():
        orders_data = [
            {
                "id": 5001,
                "number": "5001",
                "status": "completed",
                "currency": "USD",
                "date_created": "2026-10-21T14:22:00",
                "total": "128.00",
                "customer_id": 42,
                "customer_note": "Please leave package on front porch behind pillar.",
                "line_items": [
                    {
                        "id": 99,
                        "name": "Organic Linen Casual Shirt",
                        "product_id": 101,
                        "quantity": 2,
                        "total": "128.00",
                    }
                ],
            }
        ]

        notes_data = [
            {
                "id": 301,
                "author": "Fulfillment Bot",
                "date_created": "2026-10-21T15:00:00",
                "note": "Tracking ID #USPS9400 generated and dispatched.",
                "customer_note": True,
            }
        ]

        mock_session = MagicMock()

        def mock_get(url, auth=None, headers=None, params=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "/notes" in url:
                mock_resp.json = AsyncMock(return_value=notes_data)
            elif "/orders" in url:
                mock_resp.json = AsyncMock(return_value=orders_data)
            else:
                mock_resp.json = AsyncMock(return_value=[])

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = WooCommerceConnector(
            store_url=wc_config["store_url"],
            consumer_key=wc_config["consumer_key"],
            consumer_secret=wc_config["consumer_secret"],
            session=mock_session,
        )

        orders = await connector.fetch_orders(include_notes=True)
        assert len(orders) == 1
        o = orders[0]
        assert o.id == "5001"
        assert o.number == "5001"
        assert o.total == 128.0
        assert o.status == "completed"
        assert len(o.line_items) == 1
        assert len(o.order_notes) == 1
        assert o.order_notes[0].customer_note is True

        md = o.to_markdown()
        assert "# Order #5001" in md
        assert "**Total Amount:** 128.00 USD" in md
        assert "Please leave package on front porch behind pillar." in md
        assert "2x Organic Linen Casual Shirt" in md
        assert "Tracking ID #USPS9400" in md

    asyncio.run(run_test())


def test_auth_error_handling(wc_config):
    async def run_test():
        # 401 Unauthorized
        mock_session_401 = MagicMock()
        mock_resp_401 = MagicMock()
        mock_resp_401.status = 401
        cm_401 = MagicMock()
        cm_401.__aenter__ = AsyncMock(return_value=mock_resp_401)
        cm_401.__aexit__ = AsyncMock(return_value=None)
        mock_session_401.get.return_value = cm_401
        mock_session_401.closed = False

        connector_401 = WooCommerceConnector(
            store_url=wc_config["store_url"],
            consumer_key=wc_config["consumer_key"],
            consumer_secret=wc_config["consumer_secret"],
            session=mock_session_401,
        )
        with pytest.raises(PermissionError, match="WooCommerce authentication failed: Invalid Consumer Key or Secret"):
            await connector_401.fetch_products()

        # 403 Forbidden
        mock_session_403 = MagicMock()
        mock_resp_403 = MagicMock()
        mock_resp_403.status = 403
        cm_403 = MagicMock()
        cm_403.__aenter__ = AsyncMock(return_value=mock_resp_403)
        cm_403.__aexit__ = AsyncMock(return_value=None)
        mock_session_403.get.return_value = cm_403
        mock_session_403.closed = False

        connector_403 = WooCommerceConnector(
            store_url=wc_config["store_url"],
            consumer_key=wc_config["consumer_key"],
            consumer_secret=wc_config["consumer_secret"],
            session=mock_session_403,
        )
        with pytest.raises(PermissionError, match="WooCommerce authorization failed: Insufficient permissions"):
            await connector_403.fetch_products()

    asyncio.run(run_test())


def test_to_documents(wc_config):
    async def run_test():
        mock_session = MagicMock()

        def mock_get(url, auth=None, headers=None, params=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            if "/products/reviews" in url:
                mock_resp.json = AsyncMock(return_value=[])
            elif "/products" in url:
                mock_resp.json = AsyncMock(
                    return_value=[
                        {"id": 1, "name": "Ceramic Mug", "price": "14.99", "regular_price": "14.99", "status": "publish"}
                    ]
                )
            elif "/notes" in url:
                mock_resp.json = AsyncMock(return_value=[])
            elif "/orders" in url:
                mock_resp.json = AsyncMock(
                    return_value=[
                        {"id": 10, "number": "10", "total": "14.99", "currency": "USD", "status": "processing"}
                    ]
                )
            else:
                mock_resp.json = AsyncMock(return_value=[])

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = WooCommerceConnector(
            store_url=wc_config["store_url"],
            consumer_key=wc_config["consumer_key"],
            consumer_secret=wc_config["consumer_secret"],
            session=mock_session,
        )

        docs = await connector.to_documents()
        assert len(docs) == 2
        assert any("# Product: Ceramic Mug" in d for d in docs)
        assert any("# Order #10" in d for d in docs)

    asyncio.run(run_test())
