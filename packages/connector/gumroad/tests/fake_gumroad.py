"""Deterministic in-memory mock client for Gumroad API tests."""

from __future__ import annotations

from typing import Any


class FakeGumroadClient:
    """Mock client returning canned Gumroad products and user responses."""

    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[dict[str, Any]] = []

    def ping(self) -> bool:
        return not self.should_fail

    def get_products(self) -> list[dict[str, Any]]:
        self.calls.append({"endpoint": "/products"})
        if self.should_fail:
            raise Exception("Network error connecting to Gumroad API")

        return [
            {
                "id": "prod_ai_playbook_001",
                "name": "Full-Stack AI Agent Playbook",
                "preview_url": "https://gumroad.com/l/playbook",
                "description": (
                    "<p>A <b>comprehensive</b> guide to deploying autonomous agents.</p>"
                ),
                "customizable_price": False,
                "require_shipping": False,
                "custom_receipt": "Access your playbook repository here.",
                "custom_permalink": "ai-agent-playbook",
                "subscription_duration": None,
                "published": True,
                "formatted_price": "$19",
                "price": 1900,
                "currency": "usd",
                "url": "https://gumroad.com/l/playbook",
                "tags": ["ai", "agents", "python", "automation"],
                "variants": [
                    {"title": "Standard Edition", "price_difference": 0},
                    {"title": "Enterprise Edition", "price_difference": 3000},
                ],
            },
            {
                "id": "prod_ats_resume_002",
                "name": "FAANG ATS Resume Kit",
                "preview_url": "https://gumroad.com/l/ats-resume",
                "description": (
                    "<div>15 battle-tested ATS resume templates and bullet points.</div>"
                ),
                "customizable_price": False,
                "require_shipping": False,
                "published": True,
                "formatted_price": "$9",
                "price": 900,
                "currency": "usd",
                "url": "https://gumroad.com/l/ats-resume",
                "tags": ["career", "resume", "templates"],
                "variants": [],
            },
        ]
