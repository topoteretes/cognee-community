"""Security verification: ensures zero secret leakage across all emitted documents."""

from cognee_community_connector_doppler.doppler import _iter_rows
from fake_doppler import FakeDoppler


def test_zero_secret_leakage_guarantee():
    fake = FakeDoppler()
    fake.add_project("payment-gateway")
    fake.add_environment("payment-gateway", "prd", "Production")
    # Config with sensitive keys and dummy secret values in backend
    fake.add_config(
        "payment-gateway",
        "prd_main",
        "prd",
        ["STRIPE_API_SECRET_KEY", "DATABASE_PASSWORD", "JWT_SIGNING_SECRET"],
    )

    state = {}
    stats = {}
    rows = list(_iter_rows(fake, state, stats, include=("projects", "environments", "configs", "logs")))

    config_row = next(r for r in rows if r["id"] == "config:payment-gateway:prd_main")

    # Variable keys MUST be present
    assert "STRIPE_API_SECRET_KEY" in config_row["content"]
    assert "DATABASE_PASSWORD" in config_row["content"]
    assert "JWT_SIGNING_SECRET" in config_row["content"]

    # Secret values MUST NEVER appear in any document content or title!
    assert "secret_value_for" not in config_row["content"]
    assert "computed_" not in config_row["content"]

    for row in rows:
        assert "secret_value_for" not in row["content"]
        assert "computed_" not in row["content"]
