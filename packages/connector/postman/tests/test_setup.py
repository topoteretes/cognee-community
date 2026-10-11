import importlib.util
import urllib.request
from pathlib import Path

import pytest


def test_setup_refuses_redirect_without_forwarding_api_key():
    path = Path(__file__).parents[1] / "examples" / "configure_local.py"
    spec = importlib.util.spec_from_file_location("postman_setup", path)
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    request = urllib.request.Request(
        "https://api.postman.com/me", headers={"X-Api-Key": "synthetic-secret"}
    )
    for target in ("https://other.invalid/me", "https://api.postman.com/other"):
        with pytest.raises(RuntimeError, match="redirected"):
            setup.NoRedirect().redirect_request(request, None, 302, "Found", {}, target)
