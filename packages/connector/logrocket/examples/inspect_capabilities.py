"""Run read-only capability, response, or REST diagnostics from the checkout.

Example: uv run python examples/inspect_capabilities.py inspect-capabilities
    --from-file tests/fixtures/public-docs-baseline.json
"""

from diagnostics import main

if __name__ == "__main__":
    raise SystemExit(main())
