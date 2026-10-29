"""The test-account seed script must refuse live keys and attach payment methods correctly."""

import importlib.util
import pathlib
from unittest.mock import MagicMock

import pytest

SEED_PATH = pathlib.Path(__file__).parents[1] / "examples" / "seed_test_data.py"


@pytest.fixture
def seed():
    spec = importlib.util.spec_from_file_location("seed_test_data", SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_live_key_is_refused(seed, monkeypatch):
    monkeypatch.setenv("STRIPE_SEED_KEY", "sk_live_123")
    monkeypatch.setattr(seed.stripe, "StripeClient", MagicMock())

    with pytest.raises(SystemExit, match="TEST key"):
        seed.main()
    seed.stripe.StripeClient.assert_not_called()


def test_an_already_seeded_account_is_left_alone(seed, monkeypatch):
    monkeypatch.setenv("STRIPE_SEED_KEY", "sk_test_123")
    client = MagicMock()
    client.v1.customers.list.return_value.data = [MagicMock()]
    monkeypatch.setattr(seed.stripe, "StripeClient", MagicMock(return_value=client))

    with pytest.raises(SystemExit, match="already seeded"):
        seed.main()
    client.v1.products.create.assert_not_called()


def test_the_default_payment_method_is_the_attached_copy(seed):
    client = MagicMock()
    client.v1.customers.create.return_value.id = "cus_1"
    client.v1.payment_methods.attach.return_value.id = "pm_copy"

    seed.create_customer(client, "Alphacorp", "billing@alphacorp.test", "pm_card_visa")

    client.v1.payment_methods.attach.assert_called_once_with(
        "pm_card_visa", params={"customer": "cus_1"}
    )
    client.v1.customers.update.assert_called_once_with(
        "cus_1", params={"invoice_settings": {"default_payment_method": "pm_copy"}}
    )


def test_the_open_invoice_includes_its_pending_item(seed, monkeypatch):
    monkeypatch.setenv("STRIPE_SEED_KEY", "sk_test_123")
    client = MagicMock()
    client.v1.customers.list.return_value.data = []
    monkeypatch.setattr(seed.stripe, "StripeClient", MagicMock(return_value=client))

    seed.main()

    [invoice_call] = client.v1.invoices.create.call_args_list
    assert invoice_call.kwargs["params"]["pending_invoice_items_behavior"] == "include"
    client.v1.invoices.finalize_invoice.assert_called_once()
    # Alphacorp and Bravocorp subscribe to the plan; Charliecorp only has the invoice.
    assert client.v1.subscriptions.create.call_count == 2
