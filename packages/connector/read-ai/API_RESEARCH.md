# Read.ai provider contracts and connector policy

Primary sources checked on **2026-10-14**. This note separates documented facts from policies tested offline. No live account, authorization or meeting modification was performed.

## Authentication and identity

Read.ai documents OAuth 2.1 dynamic registration, authorization-code exchange with PKCE, and rotating refresh tokens. Access tokens last approximately ten minutes. Refresh should be serialized, durably persisted and immediately retried after a timeout or server error; permanent failures are not retryable by that rule. The registration fields other than `client_name` are specified by the provider. [Read.ai authentication](https://support.read.ai/hc/en-us/articles/49380809380371-API-Keys-Authentication)

The live discovery response identifies issuer `https://authn.read.ai/`, RS256 ID-token signing, JWKS `https://authn.read.ai/.well-known/jwks.json`, UserInfo `https://authn.read.ai/userinfo`, token endpoint `https://authn.read.ai/oauth2/token` and registration endpoint `https://api.read.ai/oauth/register`. The connector pins those trusted locations rather than following token-supplied URLs. [Read.ai discovery](https://authn.read.ai/.well-known/openid-configuration)

OpenID Connect requires appropriate signature, issuer, audience, expiration, nonce and authorized-party validation. UserInfo's subject must match the ID-token subject. The connector uses issuer plus subject as account identity and validates `at_hash` when supplied; PyJWT does not perform that hash comparison automatically. [OIDC ID-token validation](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation), [OIDC UserInfo validation](https://openid.net/specs/openid-connect-core-1_0.html#UserInfoResponse), [PyJWT official usage](https://pyjwt.readthedocs.io/en/stable/usage.html)

## Inventory and enrichment

`GET /v1/meetings` lists newest first with a maximum page size of ten; `has_more` drives continuation and the preceding page's final meeting ID becomes the next cursor. Filtering concerns meeting start timestamps, not modification timestamps. Detail and live-detail endpoints expand selected enrichment fields. Active meetings have no end timestamp and may lack enrichment. These properties motivate full reconciliation, not an update-time watermark. The documentation specifies a 100 requests/minute/user ceiling and distinguishes 403 from 404. [Read.ai API reference](https://support.read.ai/hc/en-us/articles/49381161088659-API-Reference)

A workspace must enable Downloads in Reports & Sharing for API access. API visibility follows reports accessible to the user; workspace-wide visibility requires additional report access. [Read.ai prerequisites](https://support.read.ai/hc/en-us/articles/49379985941523-Read-AI-API-and-MCP-Overview)

The API examples do not establish an authoritative missing/null/empty clearing contract. This connector's current-response policy withdraws unavailable selected sections, replaces metric projections, revisits pending sections and fails on malformed types. It does not label absence an intentional clear. Live evidence is still required for actual provider transitions. [API enrichment examples](https://support.read.ai/hc/en-us/articles/49381161088659-API-Reference)

## Removal and local synchronization

Read.ai owners can delete reports for everyone; nonowners can remove reports from their own list. Those UI behaviors do not document exact REST response timing for every visibility transition. Thus complete inventory plus same-attempt detail/second-inventory corroboration is connector policy. Permission errors fail synchronization and retain local data; this connector does not claim complete access-control synchronization. [Read.ai deletion documentation](https://support.read.ai/hc/en-us/articles/22408013818899-How-do-I-delete-a-meeting-report)

With dlt merge, `primary_key` identifies the logical meeting row and a boolean column needs the actual `hard_delete` hint to remove matching destination rows. Package abort restores local state/schema; it is not a general promise to roll back partial destination writes. The connector therefore revalidates upstream and repeats only its owned staging rebuild. [dlt merge](https://dlthub.com/docs/general-usage/merge-loading), [dlt pipeline API](https://dlthub.com/docs/api_reference/dlt/pipeline/)

Cognee 1.6.4's installed source was inspected for document-mode version 2, explicit resolver merge configuration, retained-table readback and scoped orphan cleanup. Staging load completion is not cognification completion, and logged cleanup failures are not proof of deletion. The connector retains an attempt and checks storage before acknowledging success. [Cognee release source](https://github.com/topoteretes/cognee/tree/v1.6.4/cognee/tasks/ingestion)

## Evidence boundaries

Synthetic OAuth/provider/model responses can verify the connector's implementation with real local storage. They cannot prove live authorization, provider edit/deletion timing, undocumented enrichment clears or real model inference. Live acceptance requires authorized synthetic meetings and recorded OAuth, edit, clear, owner-delete, list-removal and permission checks with actual Cognee cleanup.

## Reproduced Cognee 1.6.4 cleanup dependency

`tests/test_core_dependency.py` uses real dlt/SQLite/Ladybug/LanceDB and production ingestion, graph extraction and provenance cleanup. After successful ingestion of one synthetic `works_with` relationship, it injects one failure in `LanceDBAdapter.delete_data_points` for `EdgeType_relationship_name` **only after the corresponding graph node has been removed**. The core orphan EdgeType routine logs this failure nonfatally. Restoring the vector method and repeating unchanged synchronization leaves the orphan vector present. Both attempts fail verification and preserve the previous successful checkpoint. This is a reproduced core recovery dependency, not live-provider evidence.

The relevant installed Cognee 1.6.4 code is `cognee/infrastructure/databases/unified/provenance_delete_planner.py`, `_cleanup_orphaned_edge_types`: graph deletion precedes the caught vector deletion failure. Removing the graph/source references prevents a later document deletion from rediscovering the lost EdgeType evidence. A core change must persist or retry the residual cleanup intent; the connector deliberately does not introduce backend-specific repair SQL or vector-delete workarounds. [Cognee 1.6.4 cleanup source](https://github.com/topoteretes/cognee/blob/v1.6.4/cognee/infrastructure/databases/unified/provenance_delete_planner.py)
