import logging
import time
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

BREX_SOURCE_NAME = "brex"
DOCUMENT_SOURCE_ATTR = "is_document_source"

def brex_expenses(api_token: str | None = dlt.secrets.value):
    """Returns the Brex dlt source."""

    @dlt.resource(name="brex_items", write_disposition="replace")
    def _brex_resource():
        if not api_token:
            raise ValueError("API token is required for the Brex connector")

        headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json"
        }

        # Fetch expenses
        url = "https://platform.brex.com/v2/expenses"
        params = {"limit": 100}

        while True:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            for item in data.get("items", []):
                yield _expense_to_row(item)
            
            cursor = data.get("next_cursor")
            if cursor:
                params["cursor"] = cursor
            else:
                break

        # Fetch budgets
        budgets_url = "https://platform.brex.com/v2/budgets"
        b_params = {"limit": 100}
        while True:
            b_response = requests.get(budgets_url, headers=headers, params=b_params)
            if b_response.status_code == 200:
                b_data = b_response.json()
                for budget in b_data.get("items", []):
                    yield _budget_to_row(budget)
                b_cursor = b_data.get("next_cursor")
                if b_cursor:
                    b_params["cursor"] = b_cursor
                else:
                    break
            else:
                # Some tokens might not have budget access, so we just log and ignore
                logger.warning(f"Could not fetch budgets: {b_response.status_code}")
                break

    @dlt.source(name=BREX_SOURCE_NAME)
    def _brex():
        return _brex_resource()

    source = _brex()
    setattr(source, DOCUMENT_SOURCE_ATTR, BREX_SOURCE_NAME)
    return source

def _expense_to_row(item: dict) -> dict:
    merchant = item.get("merchant_name", "Unknown Merchant")
    memo = item.get("memo", "")
    category = item.get("category", "")
    amount = item.get("amount", {}).get("amount", 0)
    currency = item.get("amount", {}).get("currency", "USD")

    content = f"Merchant: {merchant}\\nMemo: {memo}\\nCategory: {category}\\nAmount: {amount} {currency}"

    return {
        "id": f"expense_{item.get('id')}",
        "title": f"Expense: {merchant}",
        "content": content
    }

def _budget_to_row(item: dict) -> dict:
    name = item.get("name", "Unknown Budget")
    desc = item.get("description", "")
    amount = item.get("limit", {}).get("amount", 0)
    currency = item.get("limit", {}).get("currency", "USD")
    period = item.get("period_type", "")

    content = f"Budget: {name}\\nDescription: {desc}\\nLimit: {amount} {currency}\\nPeriod: {period}"

    return {
        "id": f"budget_{item.get('id')}",
        "title": f"Budget: {name}",
        "content": content
    }
