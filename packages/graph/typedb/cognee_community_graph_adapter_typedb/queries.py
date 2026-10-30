"""TypeQL for the TypeDB adapter: the schema, every query template and the
builders that assemble parameterised queries.

Values reach the server through the ``given`` stage (driver ``given_rows``),
never by string interpolation, so each template compiles once and is
injection-safe. The one exception is a ref entity's IID, which ``given``
cannot bind; ``_iid_literal`` validates it before it enters query text.
"""

import json
import re
import time
from pathlib import Path

# The schema is the single source of truth in schema.tql (shipped with the
# package). The define is idempotent and re-run on every fresh adapter, so
# additive schema evolution reaches existing databases; incompatible changes
# require a fresh database.
COGNEE_SCHEMA = (Path(__file__).parent / "schema.tql").read_text(encoding="utf-8")

_SCHEMA_KEYWORDS = ("define", "undefine", "redefine")
# Word-boundary match, applied only after string literals and comments are
# stripped, so reads over e.g. `updated-at` or values like "deleted" are not
# misclassified as writes.
_WRITE_STAGE_RE = re.compile(r"\b(insert|put|update|delete)\b")
_STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_COMMENT_RE = re.compile(r"#[^\n]*")

# --- given-parameterized query templates -----------------------------------


_NODE_UPSERT = """
given $id: string, $type: string, $name: string, $props: string, $created: integer, $now: integer;
put $n isa node, has node-id == $id;
update
  $n has node-type == $type;
  $n has name == $name;
  $n has properties-json == $props;
  $n has created-at == $created;
  $n has updated-at == $now;
"""

_EDGE_UPSERT = """
given $key: string, $sid: string, $tid: string, $rel: string, $eoid: string, $props: string,
  $now: integer;
match
  $s isa node, has node-id == $sid;
  $t isa node, has node-id == $tid;
put
  $e isa edge, links (source: $s, target: $t),
    has edge-key == $key, has relationship-name == $rel;
update
  $e has edge-object-id == $eoid;
  $e has properties-json == $props;
  $e has updated-at == $now;
"""

_SET_EDGE_CREATED_AT = """
given $key: string, $now: integer;
match $e isa edge, has edge-key == $key; not { $e has created-at $c; };
insert $e has created-at == $now;
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _edge_key(source_id: str, target_id: str, relationship_name: str) -> str:
    """Edge identity as a JSON-encoded triple, so ids containing '|' cannot collide."""
    return json.dumps([source_id, target_id, relationship_name], separators=(",", ":"))


_FETCH_NODES = """
given $id: string;
match $n isa node, has node-id == $id;
fetch { "node": { $n.* } };
"""

_HAS_EDGES = """
given $key: string, $sid: string, $tid: string, $rel: string;
match $e isa edge, has edge-key == $key;
fetch { "source": $sid, "target": $tid, "relationship_name": $rel };
"""

_DELETE_INCIDENT_EDGES = """
given $id: string;
match $n isa node, has node-id == $id; $e isa edge, links ($n);
delete $e;
"""

_DELETE_NODES = """
given $id: string;
match $n isa node, has node-id == $id;
delete $n;
"""

# Provenance links of artifacts about to be deleted (TypeDB does not remove a
# relation when a role player is deleted). One template per artifact
# selection, formatted with the link relation type.
_DELETE_LINKS_OF_NODES = """
given $id: string;
match $n isa node, has node-id == $id; $l isa {link}, links (artifact: $n);
delete $l;
"""
_DELETE_LINKS_OF_INCIDENT_EDGES = """
given $id: string;
match $n isa node, has node-id == $id; $e isa edge, links ($n); $l isa {link}, links (artifact: $e);
delete $l;
"""
_DELETE_LINKS_OF_EDGES_BY_KEY = """
given $id: string;
match $e isa edge, has edge-key == $id; $l isa {link}, links (artifact: $e);
delete $l;
"""
_DELETE_LINKS_OF_LABELED_EDGES = """
given $id: string, $label: string;
match
  $n isa node, has node-id == $id;
  $e isa edge, links ({anchor_role}: $n), has relationship-name == $label;
  $l isa {link}, links (artifact: $e);
delete $l;
"""
_LINK_TYPES = ("sourced-from", "run-attached")
# Attributes earlier versions of this adapter put on node/edge for provenance.
_PRE_LINK_PROVENANCE_ATTRIBUTES = frozenset(
    {"source-ref-key", "source-dataset-id", "source-run-id", "source-run-ref", "provenance-json"}
)


def _link_deletes(template: str, **fields) -> list[str]:
    """The two link-deletion statements (one per link type) for a template."""
    return [template.format(link=link, **fields) for link in _LINK_TYPES]


# Incident edges of an anchor node, one query per role the anchor plays (an
# `or` over the role is 12-18x slower than two directional queries). Both
# produce the same document shape; self-loops
# appear in both and consumers de-duplicate by (source, target, rel). Only
# the far endpoint's document is fetched — the anchor is always already
# known to every consumer, and hub nodes would otherwise ship their payload
# once per incident edge.
_INCIDENT_EDGES_OUT = """
given $id: string;
match
  $n isa node, has node-id == $id;
  $e isa edge, links (source: $n, target: $m);
  $m has node-id $mid;
  $e has relationship-name $rel;
fetch {
  "source": $id, "target": $mid, "relationship_name": $rel,
  "edge": { $e.* }, "source_node": { "node-id": $id }, "target_node": { $m.* }
};
"""

_INCIDENT_EDGES_IN = """
given $id: string;
match
  $n isa node, has node-id == $id;
  $e isa edge, links (source: $m, target: $n);
  $m has node-id $mid;
  $e has relationship-name $rel;
fetch {
  "source": $mid, "target": $id, "relationship_name": $rel,
  "edge": { $e.* }, "source_node": { $m.* }, "target_node": { "node-id": $id }
};
"""

# incoming=True: neighbours pointing at the node; incoming=False: pointed to.
_NEIGHBOURS = """
given $id: string{label_decl};
match
  $n isa node, has node-id == $id;
  $e isa edge, links ({anchor_role}: $n, {neighbour_role}: $m){label_constraint};
  $e has relationship-name $rel;
fetch {{ "neighbour": {{ $m.* }}, "relationship_name": $rel, "node": {{ $n.* }} }};
"""

_REMOVE_LABELED_EDGES = """
given $id: string, $label: string;
match
  $n isa node, has node-id == $id;
  $e isa edge, links ({anchor_role}: $n), has relationship-name == $label;
delete $e;
"""

_ALL_NODE_IDS = "match $n isa node, has node-id $id; select $id;"
_ALL_EDGE_ENDPOINTS = """
match
  $e isa edge, links (source: $s, target: $t);
  $s has node-id $sid;
  $t has node-id $tid;
select $sid, $tid;
"""
_ALL_NODES = 'match $n isa node; fetch { "node": { $n.* } };'
_ALL_EDGES = """
match
  $e isa edge, links (source: $s, target: $t);
  $s has node-id $sid;
  $t has node-id $tid;
  $e has relationship-name $rel;
fetch { "source": $sid, "target": $tid, "relationship_name": $rel, "edge": { $e.* } };
"""
_ISOLATED_NODE_IDS = """
match
  $n isa node, has node-id $id;
  not { $e isa edge, links ($n); };
select $id;
"""


# --- provenance, weights, metadata, triplets -------------------------------

# Artifact match fragments: bind $x (node or edge) from a given $id.
_MATCH_BY_ID = {
    "node": "$x isa node, has node-id == $id;",
    "edge": "$x isa edge, has edge-key == $id;",
}
# Provenance link kinds: (ref entity type, its key attribute, its derived-id
# attribute, link relation type, the ref's role in it).
_PROVENANCE_LINKS = {
    "ref": ("source-ref", "source-ref-key", "source-dataset-id", "sourced-from", "ref"),
    "run": ("run-ref", "source-run-ref", "source-run-id", "run-attached", "run"),
}

# Ref entities are put once per batch, before the chunks that link to them.
_PUT_SOURCE_REF = """
given $k: string, $d: string;
put $r isa source-ref, has source-ref-key == $k;
update $r has source-dataset-id == $d;
"""
_PUT_RUN_REF = """
given $k: string, $run: string;
put $r isa run-ref, has source-run-ref == $k;
update $r has source-run-id == $run;
"""


def _links_read_query(kind: str, link: str) -> str:
    """One row per provenance link of each given artifact: its position and
    the ref's key. Keep the per-link fetch form: a select that joins the key
    through the ref entity, followed by link inserts in the same
    transaction, runs far slower on TypeDB 3.12."""
    _entity, key_attr, _derived, relation, role = _PROVENANCE_LINKS[link]
    return (
        "given $id: string;\n"
        f"match {_MATCH_BY_ID[kind]} $l isa {relation}, links (artifact: $x, {role}: $r);\n"
        f'fetch {{ "id": $id, "p": $l.position, "k": $r.{key_attr} }};'
    )


def _links_fetch_list(var: str, link: str) -> str:
    """A fetch sub-query listing ``var``'s links of one kind (key + position)."""
    _entity, key_attr, _derived, relation, role = _PROVENANCE_LINKS[link]
    # Own variable names: the enclosing match may bind $l / $r to other types.
    return (
        f"[ match $pl_{link} isa {relation}, links (artifact: {var}, {role}: $pr_{link});"
        f' fetch {{ "k": $pr_{link}.{key_attr}, "p": $pl_{link}.position }}; ]'
    )


def _ref_lookup_query(link: str) -> str:
    """Resolve ref entities by key, once per transaction. Links are then
    written against the entity's IID: a key lookup per row would scan, since
    every key of a dataset shares a long prefix."""
    entity, key_attr, _derived, _relation, _role = _PROVENANCE_LINKS[link]
    return f"given $v: string;\nmatch $r isa {entity}, has {key_attr} == $v;\nselect $v, $r;"


_IID_RE = re.compile(r"^0x[0-9a-f]+$")


def _iid_literal(iid: str) -> str:
    """An IID is the one value that goes into query text (the `given` stage
    cannot bind one); validate it so nothing else ever can."""
    if not _IID_RE.fullmatch(iid):
        raise ValueError(f"not a TypeDB IID: {iid!r}")
    return iid


def _link_insert_query(kind: str, link: str, ref_iid: str) -> str:
    entity, _key_attr, _derived, relation, role = _PROVENANCE_LINKS[link]
    return (
        "given $id: string, $p: integer;\n"
        f"match {_MATCH_BY_ID[kind]} $r isa {entity}, iid {_iid_literal(ref_iid)};\n"
        # insert, not put: the preceding read established the link is absent.
        f"insert (artifact: $x, {role}: $r) isa {relation}, has position == $p;"
    )


def _touch_query(kind: str) -> str:
    """Update the artifact's updated-at. Every provenance change does this
    for the artifacts it changed, so that concurrent changes to one artifact
    conflict at commit and the loser re-reads."""
    match = _MATCH_BY_ID[kind]
    return f"given $id: string, $now: integer;\nmatch {match}\nupdate $x has updated-at == $now;"


def _link_delete_query(kind: str, link: str, ref_iid: str) -> str:
    entity, _key_attr, _derived, relation, role = _PROVENANCE_LINKS[link]
    return (
        "given $id: string;\n"
        f"match {_MATCH_BY_ID[kind]} $r isa {entity}, iid {_iid_literal(ref_iid)};"
        f" $l isa {relation}, links (artifact: $x, {role}: $r);\n"
        "delete $l;"
    )


def _artifacts_by_ref_query(kind: str, link: str, by_derived: bool) -> str:
    """Artifacts linked to a ref entity, selected by its key or its derived id,
    with each artifact's full provenance links."""
    entity, key_attr, derived_attr, relation, role = _PROVENANCE_LINKS[link]
    attribute = derived_attr if by_derived else key_attr
    if kind == "node":
        return (
            "given $v: string;\n"
            f"match $r isa {entity}, has {attribute} == $v;"
            f" $l isa {relation}, links (artifact: $n, {role}: $r); $n isa node, has node-id $id;\n"
            f'fetch {{ "id": $id, "keys": {_links_fetch_list("$n", "ref")},'
            f' "runs": {_links_fetch_list("$n", "run")} }};'
        )
    return (
        "given $v: string;\n"
        f"match $r isa {entity}, has {attribute} == $v;"
        f" $l isa {relation}, links (artifact: $e, {role}: $r);"
        " $e isa edge, links (source: $s, target: $t);"
        " $s has node-id $sid; $t has node-id $tid; $e has relationship-name $rel;\n"
        'fetch { "source": $sid, "target": $tid, "relationship_name": $rel,'
        f' "keys": {_links_fetch_list("$e", "ref")}, "runs": {_links_fetch_list("$e", "run")} }};'
    )


def _properties_write_query(kind: str) -> str:
    match = _MATCH_BY_ID[kind]
    return (
        f"given $id: string, $v: string, $now: integer;\nmatch {match}\n"
        "update $x has properties-json == $v; $x has updated-at == $now;"
    )


def _properties_read_query(kind: str, all_artifacts: bool) -> str:
    if all_artifacts:
        key_attr = "node-id" if kind == "node" else "edge-key"
        artifact = "node" if kind == "node" else "edge"
        return (
            f"match $x isa {artifact}, has {key_attr} $id, has properties-json $p;\n"
            'fetch { "id": $id, "p": $p };'
        )
    return (
        "given $id: string;\n"
        f"match {_MATCH_BY_ID[kind]} $x has properties-json $p;\n"
        'fetch { "id": $id, "p": $p };'
    )


_NODE_DELETE_DATA = f"""
given $id: string;
match $n isa node, has node-id == $id;
fetch {{ "node": {{ $n.* }}, "keys": {_links_fetch_list("$n", "ref")},
        "runs": {_links_fetch_list("$n", "run")} }};
"""
_EDGE_DELETE_DATA = f"""
given $id: string;
match
  $e isa edge, has edge-key == $id, links (source: $s, target: $t);
  $s has node-id $sid; $t has node-id $tid; $e has relationship-name $rel;
fetch {{ "source": $sid, "target": $tid, "relationship_name": $rel, "edge": {{ $e.* }},
        "keys": {_links_fetch_list("$e", "ref")}, "runs": {_links_fetch_list("$e", "run")} }};
"""
_DELETE_EDGES_BY_KEY = """
given $id: string;
match $e isa edge, has edge-key == $id;
delete $e;
"""
_EDGES_BY_OBJECT_ID = """
given $v: string;
match $e isa edge, has edge-object-id == $v, has edge-key $k, has properties-json $p;
fetch { "eoid": $v, "key": $k, "p": $p };
"""
_METADATA_SET = """
given $k: string, $v: string;
put $m isa graph-metadata, has metadata-key == $k;
update $m has metadata-value == $v;
"""
_METADATA_GET = """
match $m isa graph-metadata, has metadata-key $k, has metadata-value $v;
fetch { "k": $k, "v": $v };
"""
_TRIPLETS_BATCH = """
match
  $e isa edge, links (source: $s, target: $t), has edge-key $k;
sort $k;
offset {offset};
limit {limit};
fetch {{ "start": {{ $s.* }}, "edge": {{ $e.* }}, "end": {{ $t.* }} }};
"""

# Filterable attributes promoted out of properties-json, usable server-side.
_PROMOTED_FILTER_ATTRS = {"type": "node-type", "name": "name"}
