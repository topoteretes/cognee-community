from datetime import date

import pytest


@pytest.fixture
def today() -> date:
    """The pinned "today" every test counts its months from."""
    return date(2026, 10, 18)
