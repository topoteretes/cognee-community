# Recorded local verification

Date: **2026-10-14**. Checkout: `feat/4742-read-ai-connector` in `topoteretes/cognee-community`. Changes are unstaged and uncommitted; no push, PR creation or maintainer message was performed.

## Milestones

**Offline implementation verified, with the explicit core recovery dependency below.** Provider, OAuth and model responses are synthetic. Integration tests use actual dlt 1.30.0, Cognee 1.6.4, SQLite, Ladybug and LanceDB for ingestion, cognification, deletion, provenance inspection and retrieval. Storage, ingestion and cleanup are not replaced with test doubles in those tests. Every run uses temporary storage and protected synthetic credentials outside the checkout; telemetry is disabled.

**Live provider acceptance is not verified.** No live Read.ai account or model service was used. Authorized synthetic meetings are still needed to establish OAuth setup/rotation, historical edits, field-clearing behavior, owner deletion, user-list removal, permission transitions and actual cleanup. The API-key-to-OAuth requirement change has not been confirmed with a maintainer.

**PR readiness:** this is a locally verified candidate for maintainer review with the limitations disclosed. It is not a 100% resolved or fully accepted real-provider release. Unqualified acceptance still needs the core cleanup recovery dependency resolved, authorized live-provider evidence, and agreement on the ticket's authentication requirement.

## User-perspective recheck

The follow-up verification reran the complete 148-test suite independently on all three Python versions, added one real-store acceptance test covering ten incomplete-upstream scenarios, and ran that added test on every version. Thus **149 unique tests are verified per version in two batches**. All 37 authentication tests were additionally rerun on every version after correcting the CLI's suggested source keyword from `credentials=` to the actual `credentials_path=`. The new assertion failed before that correction and passes afterward.

The new acceptance sequence starts with an actual searchable import, then checks malformed pages/sections, partial pagination, exhausted server retries, permission loss, and each of the five budget limits. Every failure preserves the complete successful journal, document IDs and chunk/vector corpus, with no new extraction/embedding work. The original meeting remains retrievable afterward.

A separate clean Python 3.12 environment installed the built wheel with the exported locked runtime dependencies, imported the package from site-packages outside the checkout, ran the installed CLI help, and ran a copied offline example from `/tmp`. It created one document and retrieved one result using real storage. Dependency consistency, lockfile consistency and whitespace checks for all 29 untracked deliverable files also passed. Build products and environments remain ignored.

Live discovery metadata and the current official provider documentation were checked again. The issuer, trusted JWKS/UserInfo endpoints and RS256 signing contract match the implementation. The default Read.ai credential file is absent and no live model credential is configured in the current process. A request for a credential-file path and authorized dedicated synthetic reports remains unanswered; no token values were requested or printed. Read-only issue inspection confirms it remains open and assigned to `ANAMASGARD`; the maintainer comment assigns the issue but does not explicitly resolve the OAuth wording.

## Results

| Check | Recorded result |
|---|---|
| Python 3.11.11 full suite, follow-up | 148 passed, 500.88 s |
| Python 3.12.15 full suite, follow-up | 148 passed, 531.51 s |
| Python 3.13.1 full suite, follow-up | 148 passed, 521.91 s |
| Added real-store upstream-failure/budget acceptance, Python 3.11 / 3.12 / 3.13 | Passed on all three, 61.11 / 61.01 / 61.10 s |
| Final authentication tests after CLI correction, Python 3.11 / 3.12 / 3.13 | 37 passed on each, 0.80 / 0.70 / 0.64 s |
| Python 3.12 integration suite | 17 passed, 467.26 s |
| Python 3.12 final lifecycle rerun | Passed, 41.90 s, including strict cognification completion status |
| Python 3.12 remaining tests, including core fault reproduction and completion status | 131 passed, 33.96 s |
| Existing Notion connector, isolated declared Cognee 1.4.0 dependencies, follow-up | 20 passed, 5.91 s |
| Existing Google Drive connector, isolated declared Cognee 1.4.0 dependencies, follow-up | 17 passed, 51.53 s |
| Ruff 0.17.0 lint and format | Passed |
| actionlint 1.7.7, dedicated workflow | Passed |
| Source distribution and wheel | Built successfully; wheel contains only package and distribution metadata |
| Installed-wheel import outside checkout, Python 3.13 | Passed; imported from site-packages; CLI help passed |
| Clean installed wheel, Python 3.12, exported locked runtime dependencies | Passed import, installed CLI help and real-store offline example from `/tmp` |
| Lockfile consistency and installed dependency consistency | Passed (`uv lock --check`, `uv pip check`) |
| Whitespace checks including new untracked deliverables | All 29 files passed |
| Offline runnable example, Python 3.12 | Passed: created one meeting document and retrieved one result |
| Live example argument parsing | Passed (`--help`); live execution not performed |

The suites emit upstream Pydantic, LanceDB and Cognee deprecation warnings. No test was skipped or marked as an expected failure. The fault reproduction below is a passing assertion that the connector detects and refuses to acknowledge an unresolved core cleanup failure.

## What the acceptance tests establish

- `test_auth.py`: signed initial identity, audience/issuer/expiry/nonce/azp/at_hash checks, matching UserInfo, refresh rotation, transient retries, protected token storage and concurrent refresh.
- `test_client.py`, `test_render.py`: complete bounded reconciliation, corroborated same-attempt removal, malformed/error responses, response/page/meeting/deadline limits, deterministic selected content, unavailable/restored sections and zero-valued/nested metrics.
- `test_source.py`, `test_sync.py`, `test_compat.py`: document markers, actual hard-delete hint, explicit merge, durable acknowledgement, configuration/account guards, cancellation settling, canonical user/dataset locks, interrupted retirement and foreground completion status.
- `test_integration.py`: actual document content and graph/vector retrieval; unchanged identities and no new model work; historical edits and old provenance/vector removal; final-document deletion; other connections, datasets and unrelated data preserved; shared nodes/edges/vectors retained; selected sections and retirement; private dataset storage; fresh-process interruption after staging/cognification/cleanup; actual pending dlt packages and a partially modified owned staging table; a second interruption during rebuilding; upstream deletion/permission loss/account replacement before recovery; two concurrent processes.

Follow-up full-suite logs are `/tmp/read-ai-reverify-311.txt`, `/tmp/read-ai-reverify-312.txt` and `/tmp/read-ai-reverify-313.txt`. Added acceptance logs are `/tmp/read-ai-reverify-{311,312,313}-new-acceptance-final.txt`; final authentication logs are `/tmp/read-ai-reverify-{311,312,313}-auth-final.txt`. Clean-install evidence is `/tmp/read-ai-reverify-wheel-install.txt` and `/tmp/read-ai-reverify-installed-offline.txt`. These are local evidence files, not tracked artifacts.

## Explicit Cognee 1.6.4 dependency

`test_core_dependency.py` injects one real vector deletion failure after core has already removed an orphan EdgeType graph node. Core catches the failure and loses the graph evidence needed to rediscover that vector. After restoring the vector method, an unchanged retry still contains the orphan vector. Both attempts raise `VerificationError`, retain the unfinished attempt and leave the last successful checkpoint unchanged.

This test proves detection and refusal to acknowledge success. It does **not** prove automatic recovery from that failure: a core cleanup change or an independently verified operator repair is required. No backend-specific vector deletion workaround is hidden in the connector. See [API_RESEARCH.md](API_RESEARCH.md#reproduced-cognee-164-cleanup-dependency) for the primary source and reproduction details.

## Reproduce locally

```bash
cd /home/linux/Desktop/cognee-community/packages/connector/read-ai
uv sync --locked
uv run pytest tests/ -q
uv run ruff check .
uv run ruff format --check .
uv run python examples/offline.py
uv build
```

The workflow runs the same checks for Python 3.11, 3.12 and 3.13 and installs the wheel before importing it from the runner's temporary directory. Hosted CI has not run yet.

## Manual Git and PR handoff

Review the package, dependency note and recorded results first. The prepared PR body is `/tmp/read-ai-4742-pr.md`. These commands have **not** been executed:

```bash
cd /home/linux/Desktop/cognee-community
git status --short --branch
git add -- packages/connector/read-ai .github/workflows/test_read_ai.yml
git diff --cached --check
git diff --cached
git commit -s -m "feat: add Read.ai connector with OAuth and scoped sync"
git push -u origin feat/4742-read-ai-connector
git rev-parse HEAD
git ls-remote origin refs/heads/feat/4742-read-ai-connector
gh pr create --repo topoteretes/cognee-community --base main \
  --head ANAMASGARD:feat/4742-read-ai-connector \
  --title "feat: add Read.ai connector" \
  --body-file /tmp/read-ai-4742-pr.md
```

The commit uses the repository's required DCO sign-off, and the PR body includes its DCO affirmation. The issue reference points to `topoteretes/cognee#4742`; the PR target is `topoteretes/cognee-community`.
