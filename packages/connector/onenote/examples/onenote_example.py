"""Select OneNote notebooks, sync them into Cognee, then ask a question.

See ../README.md for the Microsoft app registration and environment variables.
Authentication stays in this example; the connector only receives a token provider.
"""

import asyncio
import os
from pathlib import Path

import cognee
import msal

from cognee_community_connector_onenote import list_notebooks, onenote_source


def authenticate():
    """Authenticate one delegated account and return its token provider and scope."""
    client_id = os.environ.get("MICROSOFT_CLIENT_ID")
    if not client_id:
        raise RuntimeError("Set MICROSOFT_CLIENT_ID to your public-client app registration ID.")
    tenant = os.environ.get("MICROSOFT_TENANT_ID", "common")
    cache = msal.SerializableTokenCache()
    cache_file = os.environ.get("ONENOTE_TOKEN_CACHE")
    cache_path = Path(cache_file).expanduser() if cache_file else None
    if cache_path and cache_path.exists():
        cache.deserialize(cache_path.read_text())
    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant}", token_cache=cache
    )
    scopes = ["Notes.Read"]
    accounts = app.get_accounts()
    account_hint = os.environ.get("MICROSOFT_ACCOUNT_ID")
    if account_hint:
        accounts = [account for account in accounts if account["home_account_id"] == account_hint]
    if len(accounts) > 1:
        raise RuntimeError("Set MICROSOFT_ACCOUNT_ID to select one account from your token cache.")
    account = accounts[0] if accounts else None
    result = app.acquire_token_silent(scopes, account=account) if account else None
    if not result or "access_token" not in result:
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError("Microsoft could not start device authentication.")
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    if not result or "access_token" not in result:
        raise RuntimeError("Microsoft authentication failed. Sign in again and grant Notes.Read.")
    claims = result.get("id_token_claims", {})
    tenant_id = claims.get("tid")
    object_id = claims.get("oid")
    matching = [
        candidate
        for candidate in app.get_accounts()
        if candidate.get("realm") == tenant_id and candidate.get("local_account_id") == object_id
    ]
    if len(matching) != 1:
        raise RuntimeError("Could not identify the authenticated Microsoft account and tenant.")
    account = matching[0]
    if account_hint and account["home_account_id"] != account_hint:
        raise RuntimeError("Signed-in account does not match MICROSOFT_ACCOUNT_ID.")

    def save_cache():
        if cache_path and cache.has_state_changed:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.chmod(cache_path, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(cache.serialize())

    save_cache()

    def token_provider():
        refreshed = app.acquire_token_silent(scopes, account=account)
        if not refreshed or "access_token" not in refreshed:
            raise RuntimeError("Microsoft session expired. Re-run this example to sign in again.")
        save_cache()
        return refreshed["access_token"]

    account_id = f"{account['home_account_id']}:{tenant_id}"
    return token_provider, account_id


def require_completed(result, stage):
    """Reject returned errors as well as raised errors before the next stage."""
    runs = list(result.values()) if isinstance(result, dict) else [result]
    if not runs:
        raise RuntimeError(f"{stage} returned no completion result.")
    for run in runs:
        if getattr(run, "status", None) not in {
            "PipelineRunCompleted",
            "PipelineRunAlreadyCompleted",
        }:
            raise RuntimeError(f"{stage} did not complete successfully; inspect Cognee's logs.")
        for item in getattr(run, "data_ingestion_info", None) or []:
            if isinstance(item, BaseException) or (isinstance(item, dict) and item.get("error")):
                raise RuntimeError(f"{stage} returned an item failure; inspect Cognee's logs.")


async def sync(token_provider, account_id, notebook_ids, dataset_name):
    """Finish a pending DLT load, then require extraction of the current snapshot."""
    for _ in range(2):
        source = onenote_source(token_provider, notebook_ids=notebook_ids, account_id=account_id)
        result = await cognee.add(source, dataset_name=dataset_name, run_in_background=False)
        require_completed(result, "Document ingestion")
        if getattr(source, "_onenote_extracted", False):
            break
        print("Recovered a pending staging load; now extracting the current OneNote snapshot.")
    else:
        raise RuntimeError("No current OneNote snapshot was extracted. Inspect DLT's load state.")
    result = await cognee.cognify(datasets=[dataset_name], run_in_background=False)
    require_completed(result, "Graph and vector processing")


async def main():
    token_provider, account_id = authenticate()
    selected = [value.strip() for value in os.environ.get("ONENOTE_NOTEBOOK_IDS", "").split(",")]
    notebook_ids = [value for value in selected if value]
    if not notebook_ids:
        for notebook in list_notebooks(token_provider):
            print(f"{notebook['id']}\t{notebook.get('displayName', '')}")
        print("Set ONENOTE_NOTEBOOK_IDS to a comma-separated list of IDs above, then run again.")
        return
    if not os.environ.get("LLM_API_KEY"):
        raise RuntimeError("Set LLM_API_KEY for Cognee's configured LLM and embedding provider.")
    dataset_name = os.environ.get("ONENOTE_DATASET", "onenote-notes")
    await sync(token_provider, account_id, notebook_ids, dataset_name)
    answer = await cognee.recall(
        os.environ.get("ONENOTE_QUESTION", "What projects do my OneNote notes describe?"),
        datasets=[dataset_name],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
