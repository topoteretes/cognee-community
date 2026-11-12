# OpenSiro VSM Index source connector for Cognee

Import **canonically included** [OpenSiro VSM Index](https://github.com/opensiro/vsm-harness-index) assessments from a **pinned Git commit** into a **fresh local Cognee dataset**.

**Status:** Experimental source connector. Inclusion in Cognee Community is subject to upstream maintainer review.

## What it does

- Preserves the six literal S1–S5 autonomy values per included assessment. Unknown and non-established states are not guessed.
- Projects native Cognee typed graph nodes: `VSMHarness`, `VSMAssessment`, `VSMFunction`, and assessment-level `VSMEvidence`.
- Keeps the original Markdown SHA256, reviewed upstream Git SHA, pinned Index commit and Cognee-native source refs separate.
- Optionally creates six section-level `DocumentChunk` nodes for lexical BM25 search, with no LLM, embeddings or GPU requirement.
- Provides `audit_dataset` to independently check complete source identity, literal grades and node/edge ownership without changing the dataset.
- Requires a durable single-host stage-attempt marker **before any Cognee write**, so the same dataset cannot be reused after early or partial DLT ingestion failure.

## Install and test

The package targets Python 3.11–3.13 and is verified with Cognee **1.6.4**. Clean Python 3.11 and Python 3.12 checks have been performed; Python 3.13 remains unverified.

**Latest verification (B23, 2026-10-19):** [clean Python 3.11 installed-wheel CI — PASS](https://github.com/opensiro/cognee-community/actions/runs/39292809116) for the same source/test blobs as this upstream candidate: dependency installation and `pip check`, **43/43 unit tests**, pinned sharded Index validation (**583 included, 48 excluded, 12 proposed**), **3/3 real Cognee imports**, **60 graph nodes, 39 edges, 18 exact grades**, separate-process independent audit, native lexical search and pinned-source inspection, plus missing-dataset fail-closed verification. [Ruff lint and format — PASS](https://github.com/opensiro/cognee-community/actions/runs/39292816378). The validated review branch contains an OpenSiro-only workflow and evidence report; this upstream candidate contains the **same executable source and tests**, but intentionally omits those fork-specific artifacts. This is not a full-corpus Python 3.11 import or independent upstream maintainer CI.

```bash
cd packages/connector/opensiro-vsm
python -m pip install -e .
python -m unittest discover -s tests -v
```

Start with an existing local clone of the Index. Validate a pinned revision:

```bash
python -m cognee_community_connector_opensiro_vsm validate \
  --repo /path/to/vsm-harness-index \
  --commit 35bc8ab9ed4b42bb8955a6f0684a4f5529a05424
```

Then use a **new**, isolated local store:

```bash
python examples/example.py \
  --repo /path/to/vsm-harness-index \
  --commit 35bc8ab9ed4b42bb8955a6f0684a4f5529a05424 \
  --store-root ./my-new-cognee-store \
  --dataset opensiro-review-demo-v1 \
  --ids goose,langflow,openjiuwen-core --lexical
```

Replace `--ids` with `--all` to ingest every included record. The pinned Index snapshot above admitted **533** assessments, excluding **45 excluded** and **12 proposed** records.

Independent audit after import, using the same snapshot, dataset, and source tag. The audit refuses unknown dataset names rather than creating one:

```python
from cognee_community_connector_opensiro_vsm.audit import audit_dataset

# report = await audit_dataset(
#     snapshot, ("goose", "langflow"), "opensiro-review-demo-v1",
#     "opensiro_vsm_canonical_example", lexical=True
# )
# assert report["status"] == "PASS"
```

## Compatibility checks

The source review package includes `tests/test_sdk_contract.py`, which fails
if the **Cognee 1.6.4** native graph/provenance/lexical or local dataset lookup
APIs used by this connector change unexpectedly. It can be run offline with the
rest of the Python tests. These checks are API-shape checks, **not** a guarantee
that internal Cognee APIs will remain stable in future versions.

To reproduce the **clean Python 3.11 dependency-resolution check** (already
verified in a GitHub-hosted runner; the separate dev-alpha CPU host lacks
Python 3.11), use an actual Python 3.11 interpreter and the built wheel
in a brand-new virtual environment:

```bash
python3.11 -m venv /tmp/opensiro-cognee-py311
/tmp/opensiro-cognee-py311/bin/python -m pip install \
  /path/to/cognee_community_connector_opensiro_vsm-0.1.0-py3-none-any.whl
/tmp/opensiro-cognee-py311/bin/python -m pip check
/tmp/opensiro-cognee-py311/bin/python -m cognee_community_connector_opensiro_vsm \
  validate --repo /path/to/vsm-harness-index \
  --commit 35bc8ab9ed4b42bb8955a6f0684a4f5529a05424
```

**Validated environments:** The B19 Python 3.12 wheel-origin smoke reused
installed Cognee 1.6.4/DLT 1.30.0 dependencies and passed a real three-document
import and independent audit. Subsequently, the [B20 GitHub-hosted clean Python
3.11 run](https://github.com/opensiro/cognee-community/actions/runs/38942238109)
successfully built the wheel, installed all declared dependencies in a new
environment, passed `pip check`, ran **32/32 unit tests**, and completed real
three-document ingestion and a separate-process audit (69 nodes, 48 edges,
18 exact VSM function grades). B20 did not import the full corpus on Python
3.11 or certify future Cognee versions. The newer B23 verification above
covers the sharded Index and 43 tests; B20 remains historical evidence for
an earlier pinned source snapshot.

## Boundaries

This is a **fresh-only, single-host, graph-only** connector. It does not overwrite datasets, delete old records, run a hosted control plane, create embeddings, or perform automatic updates. Every attempt requires a different dataset name and the marker directory must be preserved alongside the Cognee store. Loss of that directory defeats the retry fence.

The independent audit intentionally checks a dataset already **owned by Cognee's default user** and refuses absent names; it does not create audit targets or resolve arbitrary shared-owner datasets.

Cognee's internal data-point, graph and provenance APIs have been validated **only with Cognee 1.6.4**. Lexical searches can return zero-overlap candidates; client applications should enforce a relevance guard.

The full-corpus B13 validation and B14–B16 SIGKILL studies are kept in the [original OpenSiro research branch](https://github.com/opensiro/cognee-community/tree/feat/opensiro-vsm-source/packages/connector/opensiro-vsm/docs), **not** shipped as deployment logic or destructive tests in this review branch. Research showed fail-closed detection and fresh-dataset recovery at specific checkpoints, not power-loss or arbitrary backend/distributed recovery.

### Licensing and attribution

The standalone connector package uses **Apache-2.0**, with the license text bundled in the wheel. [Cognee Community](https://github.com/topoteretes/cognee-community) and [OpenSiro Index](https://github.com/opensiro/vsm-harness-index) also declare Apache-2.0. The connector reads the Index's pinned Git tree without modification; assessment content remains attributed to its originating project, with a source URL, pinned Index commit, document SHA256 and reviewed upstream revision recorded. This does not relicense the evidence or third-party repositories linked by assessments.

OpenSiro maintains its research and review evidence separately in [opensiro/upstream](https://github.com/opensiro/upstream); this package contains the limited source connector and examples only.

## Developer walkthrough: install -> import -> search -> inspect pinned source

Use an existing local checkout of the public OpenSiro Index. Choose an **exact**
Git SHA and keep it constant for import and search. The review connector accepts
both historical flat `assessments/<id>.md` and canonical sharded
`assessments/<first-two-chars>/<id>.md` files, rejects mismatched directories
and duplicate ID paths, and retains exact provenance. The path fix is part of
this package (no out-of-tree patch required).

```bash
cd packages/connector/opensiro-vsm
python -m pip install -e .
git clone https://github.com/opensiro/vsm-harness-index /tmp/opensiro-index
INDEX_SHA=5cbb1035538b8f80fa3362f1c99652f4fd7c55e8

python -m cognee_community_connector_opensiro_vsm validate \
  --repo /tmp/opensiro-index --commit "$INDEX_SHA"
python examples/example.py \
  --repo /tmp/opensiro-index --commit "$INDEX_SHA" \
  --store-root /tmp/opensiro-cognee-example-new \
  --dataset opensiro-shards-demo-new --ids ascension,balrog,dexto --lexical
python examples/search_and_inspect.py \
  --repo /tmp/opensiro-index --commit "$INDEX_SHA" \
  --store-root /tmp/opensiro-cognee-example-new \
  --dataset opensiro-shards-demo-new \
  --query "game environment observation action feedback"
```

Choose a `harness_id` **actually present** in the resulting hits and rerun the
last command with `--inspect THAT_ID` (and optionally `--show-markdown`).
The read-only command audits an **existing** locally owned dataset, uses native
Cognee `CHUNKS_LEXICAL`, prints unfiltered ranked hits and returns the pinned
canonical assessment, SHA256, six literal autonomy labels, original evidence
bullets, and a pinned upstream Git tree link. The developer must inspect the
relevant source files there: neither a hit nor a grade proves architectural
suitability. It **does not** import or create a dataset during search.

The example intentionally uses a small sample; `--all` imports all canonically
included records at the pinned revision and requires a larger fresh local store.
Dataset names must be fresh for every import attempt; never reuse an attempted
dataset after a failed import. The indexed retrieval covers **only six VSM
function sections per assessment**; complete assessment Markdown is stored in
Cognee but other prose is not full-body-searchable. No embeddings, hosted
service, refresh sync, inference or performance ranking are implied.

**Verification boundary:** Sharded-source import, native lexical search,
independent audit and original-source inspection have been exercised with
Cognee 1.6.4 and Python 3.12 locally **and Python 3.11 on GitHub Actions**
against a three-assessment local dataset. Full Markdown is stored; only
the six function sections are indexed for lexical retrieval. This is not a
hosted-service, full-corpus Python 3.11 import, full-body-search, or
production benchmark claim. See the OpenSiro fork's [bounded independent
validation report](https://github.com/opensiro/cognee-community/blob/fix/opensiro-vsm-shards-dev-flow-20261019/packages/connector/opensiro-vsm/docs/review-validation-20261019.md) for provenance.
