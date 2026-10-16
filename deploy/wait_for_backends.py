"""Block until cognee's backends accept connections, then exit 0.

Runs in the cognee container before /app/entrypoint.sh (see the compose files).
The databases are separate compose stacks, so `depends_on` cannot order them,
and after a host reboot Docker starts every container at once: without this,
cognee's migration step hits a Postgres that is still starting, exits, and
Docker restart-loops it with growing back-off.

Checks, from the same environment cognee itself reads:
  Postgres   DB_HOST:DB_PORT, a real login as DB_USERNAME to DB_NAME
             (honours "ssl" in DATABASE_CONNECT_ARGS, e.g. {"ssl": "require"})
  ArcadeDB   GRAPH_DATABASE_URL/api/v1/ready (and VECTOR_DB_URL if different)
  S3         AWS_ENDPOINT_URL answers HTTP at all (skipped when unset = AWS)

Gives up after WAIT_FOR_BACKENDS_TIMEOUT seconds (default 600) with exit 1, so a
permanent misconfiguration (wrong password, wrong host) still surfaces as a
failing container instead of a silent hang. Never prints the password.
"""

import asyncio
import json
import os
import sys
import time

import asyncpg
import httpx

INTERVAL = 3
TIMEOUT = int(os.environ.get("WAIT_FOR_BACKENDS_TIMEOUT", "600"))


def log(msg: str) -> None:
    print(f"[wait_for_backends] {msg}", flush=True)


async def check_postgres() -> None:
    connect_args = {}
    raw = os.environ.get("DATABASE_CONNECT_ARGS")
    if raw:
        try:
            connect_args = json.loads(raw)
        except json.JSONDecodeError:
            pass
    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USERNAME", "cognee"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "cognee_db"),
        ssl=connect_args.get("ssl", "prefer"),
        timeout=5,
    )
    await conn.close()


async def check_http(url: str, *, require_ok: bool) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(url)
    if require_ok and response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")


def targets() -> dict:
    checks = {}
    if os.environ.get("DB_HOST"):
        checks[f"postgres {os.environ['DB_HOST']}:{os.environ.get('DB_PORT', '5432')}"] = check_postgres
    urls = {os.environ.get("GRAPH_DATABASE_URL"), os.environ.get("VECTOR_DB_URL")} - {None, ""}
    for url in sorted(urls):
        ready = url.rstrip("/") + "/api/v1/ready"
        checks[f"arcadedb {ready}"] = lambda u=ready: check_http(u, require_ok=True)
    s3 = os.environ.get("AWS_ENDPOINT_URL")
    if s3:
        # MinIO answers 403 at /, which is fine: the process is up.
        checks[f"s3 {s3}"] = lambda u=s3: check_http(u, require_ok=False)
    return checks


async def main() -> int:
    pending = targets()
    if not pending:
        log("nothing to wait for (no DB_HOST / GRAPH_DATABASE_URL / AWS_ENDPOINT_URL)")
        return 0
    log("waiting for: " + ", ".join(pending))
    start = time.monotonic()
    last_report: dict = {}
    while pending:
        for name, check in list(pending.items()):
            try:
                await check()
            except Exception as exc:  # noqa: BLE001 - report anything, keep polling
                reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
                if last_report.get(name) != reason:
                    log(f"not ready: {name} ({reason})")
                    last_report[name] = reason
                continue
            log(f"ready: {name}")
            del pending[name]
        if pending:
            if time.monotonic() - start > TIMEOUT:
                log(f"giving up after {TIMEOUT}s; still not ready: " + ", ".join(pending))
                return 1
            await asyncio.sleep(INTERVAL)
    log(f"all backends ready after {time.monotonic() - start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
