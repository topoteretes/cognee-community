"""Interactive local OAuth setup; credentials never appear in terminal output."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

import httpx

from .auth import (
    REDIRECT_URI,
    TokenBundle,
    default_credentials_path,
    exchange_code,
    register_client,
    save_credentials,
    verify_identity,
)
from .models import AuthenticationError


def _outside_checkout(path: Path) -> Path:
    path = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (path.parent, *path.parents)):
        raise AuthenticationError("Save OAuth credentials outside a Git checkout.")
    if path.exists():
        raise AuthenticationError("Credential file already exists; choose a new path to reconnect.")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect a Read.ai account using OAuth 2.1.")
    parser.add_argument("--credentials", type=Path, default=default_credentials_path())
    parser.add_argument("--client-name", default="Cognee Read.ai connector")
    args = parser.parse_args(argv)
    try:
        path = _outside_checkout(args.credentials)
        with httpx.Client() as client:
            registration = register_client(client, args.client_name)
            # Registration credentials cannot be retrieved later: persist before UI setup.
            save_credentials(path, registration)
            print(f"Registered OAuth client. Credentials saved securely to {path}.")
            print(f"Open {REDIRECT_URI} and enter the client ID and secret from that file.")
            print("Authorize access. Paste only the code and PKCE verifier, never a shell command.")
            code = getpass.getpass("Authorization code: ")
            verifier = getpass.getpass("PKCE verifier: ")
            nonce = getpass.getpass(
                "Expected nonce, if your authorization request used one (otherwise Enter): "
            )
            values = exchange_code(
                client,
                **registration,
                code=code,
                code_verifier=verifier,
            )
            if nonce:
                values["nonce"] = nonce
            identity = verify_identity(
                TokenBundle(
                    values["access_token"],
                    values["client_id"],
                    values.get("id_token"),
                    values.get("nonce"),
                ),
                client=client,
            )
            values["identity"] = {"issuer": identity.issuer, "subject": identity.subject}
            save_credentials(path, values)
        print(f"Read.ai connection verified. Use credentials_path={str(path)!r} in read_ai_source.")
        return 0
    except AuthenticationError as error:
        print(f"Connection setup failed: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "Connection setup cancelled. Registered credentials remain in the protected file.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
