import traceback

import pytest
from test_supabase import _Response, _Session

from cognee_community_connector_supabase import (
    SupabaseManagementAPIError,
    discover_supabase_projects,
    refresh_supabase_oauth_token,
)


def test_expired_token_explicit_refresh_and_rotation():
    with pytest.raises(SupabaseManagementAPIError, match="HTTP 401"):
        discover_supabase_projects("expired-token", session=_Session(_Response(401, {})))
    session = _Session(
        _Response(
            200,
            {
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )
    )
    token = refresh_supabase_oauth_token("client", "secret", "old-refresh", session=session)
    assert token.refresh_token == "rotated-refresh"
    assert token.expires_in == 3600
    assert session.calls[0][2]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
    }
    assert "new-access" not in repr(token)
    projects = _Session(_Response(200, []))
    assert discover_supabase_projects(token.access_token, session=projects) == []
    assert projects.calls[0][2]["headers"]["Authorization"] == "Bearer new-access"


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_failed_refresh_does_not_echo_provider_body(status):
    with pytest.raises(SupabaseManagementAPIError) as error:
        refresh_supabase_oauth_token(
            "client",
            "client-secret",
            "refresh-secret",
            session=_Session(_Response(status, {"message": "refresh-secret client-secret"})),
        )
    rendered = "".join(traceback.format_exception(error.value))
    assert "refresh-secret client-secret" not in rendered
    assert error.value.status_code == status


def test_transport_exception_chain_does_not_leak_credentials():
    class FailingSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("Bearer sensitive-access-value")

    with pytest.raises(SupabaseManagementAPIError) as error:
        discover_supabase_projects("sensitive-access-value", session=FailingSession())
    assert "Bearer sensitive-access-value" not in "".join(traceback.format_exception(error.value))


def test_invalid_json_exception_chain_is_sanitized():
    class InvalidResponse:
        status_code = 200

        def json(self):
            raise ValueError("response contains private-value")

    with pytest.raises(SupabaseManagementAPIError) as error:
        discover_supabase_projects("test", session=_Session(InvalidResponse()))
    assert "response contains private-value" not in "".join(traceback.format_exception(error.value))
