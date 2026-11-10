"""Seed a Stripe test-mode account with a small billing history to sync.

Creates three customers on one "Pro plan": one with a paid subscription, one whose
payment is disputed, and one with an open invoice. It uses Stripe's documented test
payment methods only and refuses a live key. Seeding writes, so it needs a secret test
key; run the connector itself later with a read-only restricted key. Run it once per
account: a second run would add a second copy of everything, so it stops instead.

    export STRIPE_SEED_KEY="sk_test_..."
    uv run python examples/seed_test_data.py
"""

import os
import sys

import stripe

ALPHA_EMAIL = "billing@alphacorp.test"


def create_customer(client: stripe.StripeClient, name: str, email: str, payment_method: str):
    """Create a customer whose default payment method is a copy of a Stripe test token."""
    customer = client.v1.customers.create(
        params={"name": name, "email": email, "description": "cognee connector seed"}
    )
    # Test tokens such as pm_card_visa mint a new payment method when attached, so the
    # default must be the id attach() returns, not the token.
    attached = client.v1.payment_methods.attach(payment_method, params={"customer": customer.id})
    client.v1.customers.update(
        customer.id, params={"invoice_settings": {"default_payment_method": attached.id}}
    )
    return customer


def main() -> None:
    key = os.environ.get("STRIPE_SEED_KEY", "")
    if not key.startswith("sk_test_"):
        sys.exit("Set STRIPE_SEED_KEY to a secret TEST key (sk_test_...). Live keys are refused.")
    client = stripe.StripeClient(key)
    if client.v1.customers.list(params={"email": ALPHA_EMAIL, "limit": 1}).data:
        sys.exit(
            "This account is already seeded (Alphacorp exists); seeding again would duplicate it."
        )

    product = client.v1.products.create(params={"name": "Pro plan", "description": "Seeded plan"})
    price = client.v1.prices.create(
        params={
            "product": product.id,
            "unit_amount": 4900,
            "currency": "usd",
            "recurring": {"interval": "month"},
        }
    )

    # A paid subscription: a customer, subscription, invoice and charge.
    alpha = create_customer(client, "Alphacorp", ALPHA_EMAIL, "pm_card_visa")
    client.v1.subscriptions.create(params={"customer": alpha.id, "items": [{"price": price.id}]})

    # A charge that is then disputed: a dispute linked to its charge.
    bravo = create_customer(client, "Bravocorp", "ap@bravocorp.test", "pm_card_createDispute")
    client.v1.subscriptions.create(params={"customer": bravo.id, "items": [{"price": price.id}]})

    # A finalized invoice nobody has paid yet.
    charlie = client.v1.customers.create(
        params={"name": "Charliecorp", "email": "ops@charliecorp.test"}
    )
    client.v1.invoice_items.create(
        params={
            "customer": charlie.id,
            "amount": 12000,
            "currency": "usd",
            "description": "Setup fee",
        }
    )
    # Stripe leaves pending items off a new invoice unless told to include them.
    invoice = client.v1.invoices.create(
        params={
            "customer": charlie.id,
            "collection_method": "send_invoice",
            "days_until_due": 30,
            "pending_invoice_items_behavior": "include",
        }
    )
    client.v1.invoices.finalize_invoice(invoice.id)

    print("Seeded Alphacorp (paid), Bravocorp (disputed) and Charliecorp (open invoice).")
    print("The dispute can take a moment to appear under Payments -> Disputed.")


if __name__ == "__main__":
    main()
