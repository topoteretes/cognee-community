"""Authorize a Box OAuth app and save tokens without printing them."""

import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from boxsdk import OAuth2

from cognee_community_connector_box.box import token_store


def main():
    redirect = os.environ.get("BOX_REDIRECT_URI", "http://localhost:8765/callback")
    address = urlparse(redirect)
    if address.scheme != "http" or address.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Use an HTTP loopback redirect URI for this local example")
    auth = OAuth2(
        os.environ["BOX_CLIENT_ID"],
        os.environ["BOX_CLIENT_SECRET"],
        store_tokens=token_store(os.environ["BOX_TOKEN_PATH"]),
    )
    url, expected_state = auth.get_authorization_url(redirect)
    codes = []

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            request = urlparse(self.path)
            query = parse_qs(request.query)
            received_state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            valid = (
                request.path == address.path
                and code
                and secrets.compare_digest(received_state, expected_state)
            )
            self.send_response(200 if valid else 400)
            self.end_headers()
            self.wfile.write(
                b"Authorization received. Return to the terminal."
                if valid
                else b"Invalid authorization response."
            )
            if valid:
                codes.append(code)

        def log_message(self, format, *args):
            pass  # Callback URLs contain single-use authorization codes.

    with HTTPServer((address.hostname, address.port or 80), Callback) as server:
        server.timeout = 300
        webbrowser.open(url)
        print("Approve the Box application in your browser; waiting up to five minutes.")
        server.handle_request()
    if not codes:
        raise RuntimeError("No valid OAuth callback received; rerun authorization")
    auth.authenticate(codes[0])
    print("OAuth tokens saved. Restrict access to the token file and exclude it from Git.")


if __name__ == "__main__":
    main()
