"""Measure extraction buffering with generated pages and no retained mock payloads.

Run in a separate process for each checkout/version, for example:
    uv run python tests/benchmark_contentful_memory.py --entries 10000

Provider pages contain roughly 800-byte body values. This records Python allocation
peak during extraction plus process maximum RSS; RSS includes imported dependencies.
It does not measure authenticated provider or destination load performance.
"""

import argparse
import gc
import inspect
import json
import os
import platform
import resource
import time
import tracemalloc
from importlib.metadata import version

import httpx

os.environ["TELEMETRY_DISABLED"] = "true"
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"

from cognee_community_connector_contentful import contentful_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=10000)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--payload-bytes", type=int, default=800)
    arguments = parser.parse_args()
    sync_requests = 0
    first_yield_after_requests = None

    def handle(request):
        nonlocal sync_requests
        if request.url.path.endswith("/content_types"):
            return httpx.Response(
                200,
                json={
                    "sys": {"type": "Array"},
                    "items": [],
                    "total": 0,
                    "skip": 0,
                    "limit": 100,
                },
            )
        if not request.url.path.endswith("/sync"):
            raise AssertionError(f"Unexpected benchmark endpoint: {request.url.path}")
        sync_requests += 1
        start = int(request.url.params.get("sync_token", "0"))
        end = min(start + arguments.page_size, arguments.entries)
        items = []
        for index in range(start, end):
            items.append(
                {
                    "sys": {
                        "type": "Entry",
                        "id": f"entry-{index}",
                        "space": {"sys": {"id": "benchmark"}},
                        "environment": {"sys": {"id": "master"}},
                        "contentType": {"sys": {"id": "article"}},
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "revision": 1,
                    },
                    "fields": {
                        "title": {"en-US": f"Entry {index}"},
                        "body": {"en-US": "x" * arguments.payload_bytes},
                    },
                }
            )
        cursor = "nextPageUrl" if end < arguments.entries else "nextSyncUrl"
        return httpx.Response(
            200,
            json={
                "items": items,
                cursor: (
                    "https://cdn.contentful.com/spaces/benchmark/environments/master/"
                    f"sync?sync_token={end}"
                ),
            },
        )

    service = httpx.Client(transport=httpx.MockTransport(handle))
    signature = inspect.signature(contentful_source)
    arguments_for_source = {"space_id": "benchmark", "token": "offline-benchmark-token"}
    if "service" in signature.parameters:
        arguments_for_source.update(service=service, resource_name="benchmark_entries")
    else:
        arguments_for_source["client"] = service
    # An empty model inventory is valid under either pagination protocol.
    if "resource_name" not in signature.parameters:
        original_handle = handle

        def baseline_handle(request):
            if request.url.path.endswith("/content_types"):
                return httpx.Response(200, json={"items": [], "pages": {}})
            return original_handle(request)

        service.close()
        service = httpx.Client(transport=httpx.MockTransport(baseline_handle))
        arguments_for_source["client"] = service
    source = contentful_source(**arguments_for_source)
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    count = 0
    for row in source:
        if first_yield_after_requests is None:
            first_yield_after_requests = sync_requests
        assert row["id"]
        count += 1
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    service.close()
    print(
        json.dumps(
            {
                "implementation_file": inspect.getfile(contentful_source),
                "python": platform.python_version(),
                "dependencies": {name: version(name) for name in ("cognee", "dlt", "httpx")},
                "entries": arguments.entries,
                "page_size": arguments.page_size,
                "payload_body_bytes": arguments.payload_bytes,
                "yielded_rows": count,
                "sync_requests": sync_requests,
                "first_yield_after_sync_requests": first_yield_after_requests,
                "python_peak_mib": round(peak / (1024 * 1024), 2),
                "process_peak_rss_mib": round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2
                ),
                "elapsed_seconds": round(elapsed, 2),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
