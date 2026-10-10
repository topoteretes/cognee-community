"""Live rate-limit check (read-only): fire a burst of requests with the throttle disabled.

Deel allows 5 requests/second per organisation and sends no rate-limit headers, so a burst
should produce some 429s. This shows how many 429s the API really returned and whether the
connector's retry/backoff still delivered every response. Costs nothing; reads one record per
request (``GET /contracts?limit=1``) and prints only status counts.

    export DEEL_API_TOKEN=...   DEEL_BASE_URL=https://api-staging.letsdeel.com/rest
    uv run python examples/check_rate_limit.py
"""

import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from cognee_community_connector_deel.deel import DEEL_SANDBOX_URL, _Api, _build_session

REQUESTS = 40
WORKERS = 10


def main() -> None:
    raw = Counter()  # every HTTP response, including the ones the retry layer swallowed
    session = _build_session(min_interval=0, jitter=0, backoff_factor=1.0)
    session.hooks["response"].append(lambda response, *a, **kw: raw.update([response.status_code]))
    api = _Api(
        os.environ.get("DEEL_BASE_URL", DEEL_SANDBOX_URL), os.environ["DEEL_API_TOKEN"], session
    )

    with ThreadPoolExecutor(WORKERS) as pool:
        final = Counter(pool.map(lambda _: api.probe("/contracts"), range(REQUESTS)))

    print(f"RESULT raw responses from Deel: {dict(sorted(raw.items()))}")
    print(f"RESULT final results after retries: {dict(sorted(final.items()))}")
    failed = sum(final.values()) - final.get(200, 0)
    print(f"RESULT 429s seen: {raw.get(429, 0)}; requests that still failed: {failed}")


if __name__ == "__main__":
    main()
