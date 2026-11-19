"""Read-only smoke test of every unverified Outreach assumption, for use with real credentials.

    export OUTREACH_CLIENT_ID=... OUTREACH_CLIENT_SECRET=...
    uv run outreach-auth                       # once
    uv run python examples/verify_live.py [--force-refresh] [--base-url URL]

Only GET requests are made (plus the OAuth token endpoint when ``--force-refresh`` is given,
which exercises refresh-token rotation and persists the result exactly like a normal sync).
Prints a PASS / FAIL / WARN checklist. Tokens and record *content* are never printed — only
HTTP statuses, counts and attribute *names*. Each line names the constant in
``cognee_community_connector_outreach/outreach.py`` to change if it FAILs. Exits 1 on any FAIL.
"""

import argparse
import os
import sys
import time
from urllib.parse import urlparse

from cognee_community_connector_outreach import outreach as o

RESULTS: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def get(api, path, params=None):
    response = api._client.get(path, params=params or {})
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response, body if isinstance(body, dict) else {}


def attr_names(record_):
    attrs = record_.get("attributes") if isinstance(record_, dict) else None
    return set(attrs) if isinstance(attrs, dict) else set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get(o.BASE_URL_ENV, o.DEFAULT_BASE_URL))
    parser.add_argument("--force-refresh", action="store_true", help="also test token refresh")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    try:
        auth = o.build_auth()
    except o.OutreachError as exc:
        print(f"[FAIL] credentials — {exc}")
        return 1

    # -- token lifetime / refresh / rotation ----------------------------------------------
    store = o.TokenStore().load()
    if not store:
        record("FAIL", "token store", f"none at {o.resolve_token_store_path()}; run outreach-auth")
        return 1
    remaining = float(store.get("expires_at", 0)) - time.time()
    record("PASS", "token store readable", f"access token expires in {remaining / 60:.0f} min")
    if args.force_refresh:
        before = store.get("refresh_token")
        try:
            auth.access_token(force_refresh=True)
            after = o.TokenStore().load()
            rotated = after.get("refresh_token") != before
            record(
                "PASS",
                "token refresh",
                f"refresh token {'ROTATES' if rotated else 'is reused'} "
                f"(rotation persisted: {o.TokenStore().path})",
            )
        except o.OutreachError as exc:
            record("FAIL", "token refresh", f"{exc} [SEND_REDIRECT_URI_ON_REFRESH?]")
            return 1
    else:
        record("WARN", "token refresh/rotation", "skipped; re-run with --force-refresh")

    api = o._Api(base, auth, o._build_session())

    # -- endpoints, scopes, fieldsets, sorting, filters, paging, count -------------------
    for stream in o.STREAMS.values():
        tag = stream.name
        fields = list(
            o.allowed_attributes(
                stream,
                o._Config(
                    resources=o.ALL_RESOURCES,
                    sequence_ids=(),
                    sequence_states=(),
                    activity_types=o.ALL_ACTIVITY_TYPES,
                    activity_since=o.timedelta(days=90),
                    include_contact_details=True,
                    include_activity_bodies=True,
                    page_size=2,
                    overlap_seconds=1,
                    sweep_every_n_runs=1,
                    full_reconcile=False,
                    max_delete_ratio=0.5,
                    force_delete=False,
                    skipped_table=False,
                ),
            )
        )
        params = {
            o.PARAM_PAGE_SIZE: 2,
            o.PARAM_COUNT: "false",
            o.PARAM_SORT: o.SORT_UPDATED_ASC,
            f"fields[{stream.api_type}]": ",".join(
                fields
                + (list(stream.relationships) if o.SPARSE_FIELDSETS_INCLUDE_RELATIONSHIPS else [])
            ),
        }
        response, body = get(api, stream.path, params)
        if response.status_code == 403:
            record(
                "FAIL",
                f"{tag}: endpoint + scope",
                f"HTTP 403; scope {stream.scopes} [STREAMS.scopes]",
            )
            continue
        if response.status_code == 404:
            record("FAIL", f"{tag}: endpoint", f"HTTP 404 [STREAMS['{tag}'].path]")
            continue
        if response.status_code != 200:
            record(
                "FAIL",
                f"{tag}: list request",
                f"HTTP {response.status_code}; check fields[{stream.api_type}] "
                "[STREAMS.api_type / SPARSE_FIELDSETS_INCLUDE_RELATIONSHIPS / attribute names]",
            )
            continue
        data = body.get("data") or []
        record(
            "PASS",
            f"{tag}: endpoint, scope, sparse fieldset, sort, count=false",
            f"HTTP 200, {len(data)} record(s) on page 1",
        )
        if "count" in (body.get("meta") or {}):
            record("WARN", f"{tag}: count=false", "meta.count present despite count=false")
        if not data:
            record("WARN", f"{tag}: deeper checks", "no records in this account; skipped")
            continue

        seen = set().union(*(attr_names(r) for r in data))
        missing = sorted(set(fields) - seen - {"createdAt", "updatedAt"})
        record(
            "WARN" if missing else "PASS",
            f"{tag}: allowlisted attribute names",
            f"requested-but-absent on sampled records: {missing or 'none'} "
            "[STREAMS.attributes/contact/bodies]",
        )
        rel_ok = all(
            f"{o.to_snake(r)}_id" in o.flatten_resource(data[0])
            or f"{o.to_snake(r)}_ids" in o.flatten_resource(data[0])
            for r in stream.relationships
        )
        record(
            "PASS" if rel_ok else "WARN",
            f"{tag}: relationship ids inline",
            "present for all declared relationships"
            if rel_ok
            else "some relationships lack inline data [STREAMS.relationships / "
            "SPARSE_FIELDSETS_INCLUDE_RELATIONSHIPS]",
        )

        ts = [r.get("attributes", {}).get("updatedAt") for r in data]
        if len(ts) == 2 and all(ts):
            parsed = [o.parse_ts(t) for t in ts]
            record(
                "PASS" if parsed[0] <= parsed[1] else "FAIL",
                f"{tag}: sort=updatedAt ascending",
                "[SORT_UPDATED_ASC]" if parsed[0] > parsed[1] else "",
            )

        # filter syntax + inclusive lower bound (ties)
        first_id, first_ts = data[0].get("id"), ts[0]
        if first_ts:
            floor = o.format_ts(o.parse_ts(first_ts))
            r2, b2 = get(
                api,
                stream.path,
                {
                    o.PARAM_UPDATED_AT_FILTER: f"{floor}..inf",
                    o.PARAM_PAGE_SIZE: 50,
                    o.PARAM_COUNT: "false",
                    o.PARAM_SORT: o.SORT_UPDATED_ASC,
                },
            )
            ids_ = [d.get("id") for d in b2.get("data", [])]
            if r2.status_code != 200:
                record(
                    "FAIL",
                    f"{tag}: filter[updatedAt]=<ts>..inf",
                    f"HTTP {r2.status_code} [PARAM_UPDATED_AT_FILTER / TIMESTAMP_FORMAT]",
                )
            elif first_id in ids_:
                record("PASS", f"{tag}: filter[updatedAt] lower bound is inclusive (ties kept)")
            else:
                record(
                    "WARN",
                    f"{tag}: filter[updatedAt] lower bound is EXCLUSIVE",
                    "records sharing the cursor timestamp would be missed; raise overlap_seconds",
                )

        # sweep fieldset
        r3, b3 = get(
            api,
            stream.path,
            {
                o.PARAM_PAGE_SIZE: 2,
                o.PARAM_COUNT: "false",
                f"fields[{stream.api_type}]": ",".join(o.SWEEP_FIELDS),
            },
        )
        names = set().union(*(attr_names(r) for r in b3.get("data", []))) if r3.ok else {"?"}
        record(
            "PASS" if r3.ok and names <= set(o.SWEEP_FIELDS) else "FAIL",
            f"{tag}: id-only sweep fieldset",
            f"attributes returned: {sorted(names)} [SWEEP_FIELDS]",
        )

        # pagination via links.next
        nxt = (body.get("links") or {}).get("next")
        if nxt:
            same_host = urlparse(nxt).netloc in ("", urlparse(base).netloc)
            r4 = api._client.get(nxt)
            ids2 = {d.get("id") for d in (r4.json().get("data") or [])} if r4.ok else set()
            overlap = ids2 & {d.get("id") for d in data}
            record(
                "PASS" if r4.ok and same_host and not overlap else "FAIL",
                f"{tag}: pagination via links.next",
                f"HTTP {r4.status_code}, same host: {same_host}, overlap: {len(overlap)} "
                "[NEXT_LINK_PATH]",
            )
        else:
            record("WARN", f"{tag}: links.next", "absent on page 1 (fewer records than page size?)")

    # -- max page size (once, on the cheapest stream) ---------------------------------------
    r5, b5 = get(api, "/sequences", {o.PARAM_PAGE_SIZE: o.MAX_PAGE_SIZE, o.PARAM_COUNT: "false"})
    got = len(b5.get("data") or [])
    if r5.status_code == 200:
        capped = got < o.MAX_PAGE_SIZE and (b5.get("links") or {}).get("next")
        record(
            "WARN" if capped else "PASS",
            f"page[size]={o.MAX_PAGE_SIZE} accepted",
            f"server returned {got} per page{' (CAPPED: lower MAX_PAGE_SIZE)' if capped else ''}",
        )
    else:
        record("FAIL", f"page[size]={o.MAX_PAGE_SIZE}", f"HTTP {r5.status_code} [MAX_PAGE_SIZE]")

    # -- rate limit headers (names only) -----------------------------------------------------
    hdrs = sorted(h for h in r5.headers if "rate" in h.lower() or h.lower() == "retry-after")
    record(
        "PASS" if hdrs else "WARN",
        "rate-limit headers",
        f"present: {hdrs or 'none'} "
        f"[RATE_LIMIT_PER_HOUR={o.RATE_LIMIT_PER_HOUR} is an assumption; "
        "429/Retry-After not provoked]",
    )

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    print(
        f"\n{len(RESULTS)} checks: {sum(r[0] == 'PASS' for r in RESULTS)} pass, "
        f"{sum(r[0] == 'WARN' for r in RESULTS)} warn, {len(fails)} fail"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
