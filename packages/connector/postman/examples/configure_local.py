"""Save Postman test credentials privately; no dependencies required.

Run in your own Terminal. The API key is entered using a hidden prompt and is
never passed as a command argument or printed. Only read-only API calls are made.
"""

import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

CONFIG_PATH = Path.home() / ".config" / "cognee-postman-4711" / "credentials.json"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Postman redirected the request; no credentials saved.")


def get(api_key, path):
    request = urllib.request.Request(
        "https://api.postman.com" + path,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "cognee-postman-setup/0.1",
        },
    )
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        # Classify a bounded response internally; never echo provider text, keys
        # or headers, which may contain sensitive values.
        body = exc.read(8192).decode("utf-8", errors="replace").lower()
        if "isn’t available in your region" in body or "isn't available in your region" in body:
            reason = "Provider reports regional feature restriction."
        elif "invalidapikey" in body or "invalid api key" in body:
            reason = "Provider reports an invalid API key."
        elif "expired" in body:
            reason = "Provider response indicates expiry."
        elif "service limit" in body or "monthly" in body:
            reason = "Provider response indicates a usage limit."
        elif "<html" in body or "<!doctype html" in body:
            reason = "Received an HTML denial page rather than an API JSON response."
        else:
            reason = "Provider reason is unclassified; response body withheld."
        endpoint = path if path in {"/me", "/workspaces"} else "/workspaces/{id}"
        raise RuntimeError(
            f"Postman GET {endpoint} returned HTTP {exc.code}. {reason} No credentials saved."
        ) from None
    except (urllib.error.URLError, ValueError):
        raise RuntimeError("Postman connection/JSON failed; no credentials saved.") from None
    if not isinstance(payload, dict) or payload.get("error"):
        raise RuntimeError("Postman returned an error; no credentials saved.")
    return payload


def main():
    if not sys.stdin.isatty():
        raise RuntimeError("Run this helper interactively in your own Terminal.")
    if CONFIG_PATH.exists():
        raise RuntimeError(f"Configuration already exists at {CONFIG_PATH}; refusing to overwrite.")
    api_key = getpass.getpass("Paste Postman API key (hidden): ").strip()
    if not api_key:
        raise RuntimeError("No API key entered.")
    account = get(api_key, "/me").get("user", {})
    account_id = account.get("id")
    if account_id is None:
        raise RuntimeError("Could not determine account ID; no credentials saved.")
    workspaces = get(api_key, "/workspaces").get("workspaces")
    if not isinstance(workspaces, list) or not workspaces:
        raise RuntimeError("No workspaces returned. Create the dedicated test workspace first.")
    print("Available workspaces (names and IDs only):")
    for workspace in workspaces:
        print(f"  {workspace.get('name', '')}: {workspace.get('id', '')}")
    workspace_id = input("Paste the dedicated test workspace ID from this list: ").strip()
    if workspace_id not in {entry.get("id") for entry in workspaces}:
        raise RuntimeError("Workspace ID not in returned list; no credentials saved.")
    workspace = get(api_key, f"/workspaces/{quote(workspace_id, safe='')}").get("workspace", {})
    if workspace.get("id") != workspace_id:
        raise RuntimeError("Workspace identity/access failed; no credentials saved.")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(CONFIG_PATH.parent, 0o700)
    descriptor = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(
            {
                "POSTMAN_API_KEY": api_key,
                "POSTMAN_WORKSPACE_ID": workspace_id,
                "POSTMAN_ACCOUNT_ID": str(account_id),
            },
            file,
        )
        file.write("\n")
    print(f"Saved private configuration: {CONFIG_PATH}")
    print("Permissions: directory 700, file 600. API key was not printed.")
    print("Tell Codex: configuration saved. Do not paste the file contents.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
