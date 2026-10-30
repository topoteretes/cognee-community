"""Graph-native provenance for the TypeDB adapter (cognee's
``attach_*_source_refs`` / ``find_*_by_*`` / ``get_*_delete_data`` family).

State transitions delegate to cognee's ``provenance_after_attach`` /
``provenance_after_remove``. Storage is relational: one ``source-ref``
entity per source ref key and one ``run-ref`` entity per run ref, linked to
their artifacts by ``sourced-from`` / ``run-attached`` relations whose
``position`` records the attach order. A change puts the ref entities it
needs, then, per chunk and in one transaction, reads the artifact's links,
applies the transition, links or unlinks the difference and updates the
artifact's ``updated-at`` (so concurrent changes to one artifact conflict at
commit and the loser re-reads).

``ProvenanceMixin`` is mixed into ``TypeDBAdapter`` and uses its transaction
and conversion primitives.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from cognee.infrastructure.databases.provenance import (
    EdgeDeleteData,
    EdgeIdentity,
    NodeDeleteData,
    get_dataset_id_from_source_ref_key,
    get_pipeline_run_id_from_source_run_ref,
    get_source_ref_key_from_source_run_ref,
    make_source_run_ref,
)
from cognee.infrastructure.databases.provenance.source_ref_state import (
    ProvenanceColumns,
    coerce_run_uuid,
    derive_dataset_ids,
    derive_run_ids,
    provenance_after_attach,
    provenance_after_remove,
)

from .queries import (
    _DELETE_EDGES_BY_KEY,
    _DELETE_LINKS_OF_EDGES_BY_KEY,
    _EDGE_DELETE_DATA,
    _METADATA_GET,
    _METADATA_SET,
    _NODE_DELETE_DATA,
    _PUT_RUN_REF,
    _PUT_SOURCE_REF,
    _artifacts_by_ref_query,
    _edge_key,
    _link_delete_query,
    _link_deletes,
    _link_insert_query,
    _links_read_query,
    _now_ms,
    _ref_lookup_query,
    _touch_query,
)


@dataclass(frozen=True)
class _ProvenanceAttach:
    """One provenance change: the transition to apply per artifact, plus the
    ref entities the change may link to (put before the chunks run)."""

    transition: Any
    keys: list[str] = field(default_factory=list)
    run_refs: list[str] = field(default_factory=list)

    @classmethod
    def for_keys(cls, keys: list[str], run: str | None) -> "_ProvenanceAttach":
        run_refs = [make_source_run_ref(run, key) for key in keys] if run else []
        return cls(
            lambda current, refs: provenance_after_attach(current, refs, keys, run),
            keys,
            run_refs,
        )

    @classmethod
    def removing(cls, keys: list[str]) -> "_ProvenanceAttach":
        return cls(lambda current, refs: provenance_after_remove(current, refs, keys))


class ProvenanceMixin:
    """The provenance contract, on top of TypeDBAdapter's primitives."""

    # ------------------------------------------------------------------
    # Read-modify-write primitives (one transaction each, retried on STC2)
    # ------------------------------------------------------------------

    @staticmethod
    def _ordered_links(entries) -> list[str]:
        """Keys of link entries ``{"k": key, "p": position}`` in attach order."""
        return [entry["k"] for entry in sorted(entries or [], key=lambda entry: entry["p"])]

    @classmethod
    def _decode_provenance(cls, document: dict) -> tuple[list[str], list[str]]:
        """(ordered source ref keys, ordered run refs) from a document carrying
        ``keys`` / ``runs`` link lists."""
        return cls._ordered_links(document.get("keys")), cls._ordered_links(document.get("runs"))

    @staticmethod
    def _ref_put_specs(keys, run_refs) -> list[tuple[str, list[dict]]]:
        specs = []
        if keys:
            specs.append(
                (
                    _PUT_SOURCE_REF,
                    [
                        {"k": key, "d": str(get_dataset_id_from_source_ref_key(key))}
                        for key in sorted(keys)
                    ],
                )
            )
        if run_refs:
            specs.append(
                (
                    _PUT_RUN_REF,
                    [
                        {"k": ref, "run": str(get_pipeline_run_id_from_source_run_ref(ref))}
                        for ref in sorted(run_refs)
                    ],
                )
            )
        return specs

    @staticmethod
    def _rows_by_ref(link_rows: list[dict]) -> dict[str, list[dict]]:
        """Group link rows by ref value, dropping the value from each row."""
        grouped: dict[str, list[dict]] = {}
        for row in link_rows:
            grouped.setdefault(row["v"], []).append({k: v for k, v in row.items() if k != "v"})
        return grouped

    @classmethod
    def _resolve_refs(cls, tx, link: str, values: set[str]) -> dict[str, str]:
        """IIDs of the ref entities for ``values`` (absent ones are omitted)."""
        if not values:
            return {}
        rows = [{"v": value} for value in sorted(values)]
        answer = tx.query(_ref_lookup_query(link), given_rows=rows).resolve()
        return {row["v"]: row["r"] for row in cls._collect_answer(answer)}

    async def _ensure_provenance_refs(self, attach: "_ProvenanceAttach") -> None:
        """Put the ref entities a batch will link to, once, before its chunks
        (idempotent; retried, since two batches may create one key at once)."""
        specs = self._ref_put_specs(attach.keys, attach.run_refs)
        if specs:
            await self._write_batch(specs)

    def _provenance_change_sync(self, kind: str, identities: list[str], transition, pre_specs=()):
        """One WRITE transaction: run ``pre_specs`` (a chunk's upserts), read
        each artifact's links, apply ``transition``, link/unlink the
        difference, touch the artifacts, commit. New links get positions
        after the artifact's highest existing one, so order survives
        removals."""
        from typedb.driver import TransactionType

        driver = self._get_driver()
        with driver.transaction(self.database_name, TransactionType.WRITE) as tx:
            for query_text, given_rows in pre_specs:
                tx.query(query_text, given_rows=given_rows).resolve()
            rows = [{"id": identity} for identity in identities]
            current: dict[str, dict[str, list]] = {
                identity: {"ref": [], "run": []} for identity in identities
            }
            for link in ("ref", "run"):
                for entry in self._collect_answer(
                    tx.query(_links_read_query(kind, link), given_rows=rows).resolve()
                ):
                    current[entry["id"]][link].append(entry)
            inserts: dict[str, list[dict]] = {"ref": [], "run": []}
            deletes: dict[str, list[dict]] = {"ref": [], "run": []}
            for identity, links in current.items():
                keys = self._ordered_links(links["ref"])
                run_refs = self._ordered_links(links["run"])
                columns = transition(keys, run_refs)
                for link, old, new in (
                    ("ref", links["ref"], columns.source_ref_keys),
                    ("run", links["run"], columns.source_run_refs),
                ):
                    old_keys = {entry["k"] for entry in old}
                    next_position = max((entry["p"] for entry in old), default=-1) + 1
                    for value in new:
                        if value not in old_keys:
                            inserts[link].append({"id": identity, "v": value, "p": next_position})
                            next_position += 1
                    for value in sorted(old_keys - set(new)):
                        deletes[link].append({"id": identity, "v": value})
            changed = {
                row["id"] for rows_ in (*deletes.values(), *inserts.values()) for row in rows_
            }
            for link in ("ref", "run"):
                by_ref_delete = self._rows_by_ref(deletes[link])
                by_ref_insert = self._rows_by_ref(inserts[link])
                iids = self._resolve_refs(tx, link, set(by_ref_delete) | set(by_ref_insert))
                for value, link_rows in by_ref_delete.items():
                    if value in iids:  # no entity means no link to remove
                        tx.query(
                            _link_delete_query(kind, link, iids[value]), given_rows=link_rows
                        ).resolve()
                # An insert whose ref entity is missing would match nothing and
                # commit an unowned artifact, so a missing ref is an error.
                missing = sorted(set(by_ref_insert) - set(iids))
                if missing:
                    raise RuntimeError(
                        f"provenance ref entities missing for {link} links: {missing[:3]}"
                    )
                for value, link_rows in by_ref_insert.items():
                    tx.query(
                        _link_insert_query(kind, link, iids[value]), given_rows=link_rows
                    ).resolve()
            if changed:
                now = _now_ms()
                tx.query(
                    _touch_query(kind), given_rows=[{"id": i, "now": now} for i in sorted(changed)]
                ).resolve()
            tx.commit()

    async def _provenance_change(self, kind: str, identities, attach: "_ProvenanceAttach") -> None:
        """Explicit attach/remove: put the ref entities, then apply
        ``attach.transition`` to ``identities`` in concurrent chunk-sized
        transactions, each retried on commit conflicts."""
        identities = list(dict.fromkeys(str(identity) for identity in identities))
        if not identities:
            return
        await self._provision_database()
        await self._ensure_provenance_refs(attach)
        size = self._chunk_rows
        tasks = [
            asyncio.create_task(
                self._write_chunk([], (kind, identities[start : start + size], attach.transition))
            )
            for start in range(0, len(identities), size)
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @staticmethod
    def _validated_pipeline_run_id(pipeline_run_id) -> str | None:
        """Validate a pipeline run id (UUID or its string form) before any
        server contact; cognee's transition would otherwise fail mid-transaction."""
        if pipeline_run_id is None:
            return None
        try:
            return str(coerce_run_uuid(pipeline_run_id))
        except ValueError as error:
            raise ValueError(f"pipeline_run_id must be a UUID, got {pipeline_run_id!r}") from error

    @classmethod
    def _attach_for_batch(cls, source_ref_key: str | None, pipeline_run_id):
        """The provenance attach folded into add_nodes/add_edges, or None
        without a ``source_ref_key``."""
        if source_ref_key is None:
            return None
        try:
            get_dataset_id_from_source_ref_key(source_ref_key)
        except ValueError as error:
            raise ValueError(
                "source_ref_key must be built with cognee's make_source_ref_key()"
            ) from error
        run = cls._validated_pipeline_run_id(pipeline_run_id)
        return _ProvenanceAttach.for_keys([source_ref_key], run)

    # ------------------------------------------------------------------
    # Graph provenance (cognee's 15-method contract) and parity methods
    # ------------------------------------------------------------------

    @staticmethod
    def _edge_identity_key(edge: EdgeIdentity) -> str:
        return _edge_key(str(edge.source_id), str(edge.target_id), edge.relationship_name)

    async def attach_node_source_refs(self, node_ids, source_ref_keys, pipeline_run_id=None):
        if not source_ref_keys:
            return
        run = self._validated_pipeline_run_id(pipeline_run_id)
        await self._provenance_change(
            "node", node_ids, _ProvenanceAttach.for_keys(list(source_ref_keys), run)
        )

    async def attach_edge_source_refs(self, edges, source_ref_keys, pipeline_run_id=None):
        if not source_ref_keys:
            return
        run = self._validated_pipeline_run_id(pipeline_run_id)
        await self._provenance_change(
            "edge",
            [self._edge_identity_key(edge) for edge in edges],
            _ProvenanceAttach.for_keys(list(source_ref_keys), run),
        )

    async def remove_node_source_refs(self, node_ids, source_ref_keys):
        if not source_ref_keys:
            return
        await self._provenance_change(
            "node", node_ids, _ProvenanceAttach.removing(list(source_ref_keys))
        )

    async def remove_edge_source_refs(self, edges, source_ref_keys):
        if not source_ref_keys:
            return
        await self._provenance_change(
            "edge",
            [self._edge_identity_key(edge) for edge in edges],
            _ProvenanceAttach.removing(list(source_ref_keys)),
        )

    async def delete_edge_triples(self, edges) -> None:
        """Delete the given edges only; their endpoint nodes are kept."""
        if not edges:
            return
        keys = dict.fromkeys(self._edge_identity_key(edge) for edge in edges)
        rows = [{"id": key} for key in keys]
        await self._write_batch(
            [(query, rows) for query in _link_deletes(_DELETE_LINKS_OF_EDGES_BY_KEY)]
            + [(_DELETE_EDGES_BY_KEY, rows)]
        )

    def _provenance_columns_from_document(self, document: dict) -> ProvenanceColumns:
        keys, run_refs = self._decode_provenance(document)
        return ProvenanceColumns(keys, derive_dataset_ids(keys), derive_run_ids(run_refs), run_refs)

    async def get_node_delete_data(self, node_ids) -> dict[str, NodeDeleteData]:
        if not node_ids:
            return {}
        rows = [{"id": str(node_id)} for node_id in dict.fromkeys(node_ids)]
        documents = (await self._read_batch([(_NODE_DELETE_DATA, rows)]))[0]
        result: dict[str, NodeDeleteData] = {}
        for document in documents:
            node_doc = document["node"]
            node_id = node_doc["node-id"]
            properties = self._document_to_node_dict(node_doc)
            metadata = properties.get("metadata") or {}
            indexed_fields = (
                list(metadata.get("index_fields") or []) if isinstance(metadata, dict) else []
            )
            columns = self._provenance_columns_from_document(document)
            result[node_id] = NodeDeleteData(
                node_id=node_id,
                node_type=str(properties.get("type") or node_doc.get("node-type") or ""),
                indexed_fields=indexed_fields,
                node_properties=properties,
                source_ref_keys=columns.source_ref_keys,
                source_dataset_ids=columns.source_dataset_ids,
                source_run_ids=columns.source_run_ids,
                source_run_refs=columns.source_run_refs,
            )
        return result

    async def get_edge_delete_data(self, edges) -> dict[EdgeIdentity, EdgeDeleteData]:
        if not edges:
            return {}
        # Lazy import: the modules layer imports get_graph_engine at package
        # load, which would form a cycle with this adapter module.
        from cognee.modules.graph.utils.prepare_edges_for_storage import get_edge_retrieval_text

        rows = [{"id": self._edge_identity_key(edge)} for edge in edges]
        documents = (await self._read_batch([(_EDGE_DELETE_DATA, rows)]))[0]
        result: dict[EdgeIdentity, EdgeDeleteData] = {}
        for document in documents:
            edge = EdgeIdentity(
                document["source"], document["target"], document["relationship_name"]
            )
            properties = self._document_to_edge_properties(document["edge"])
            columns = self._provenance_columns_from_document(document)
            result[edge] = EdgeDeleteData(
                edge=edge,
                edge_text=get_edge_retrieval_text(
                    properties.get("edge_text"), edge.relationship_name
                ),
                edge_properties=properties,
                source_ref_keys=columns.source_ref_keys,
                source_dataset_ids=columns.source_dataset_ids,
                source_run_ids=columns.source_run_ids,
                source_run_refs=columns.source_run_refs,
            )
        return result

    async def _artifacts_by_ref(self, kind: str, link: str, by_derived: bool, value: str):
        """Artifact documents linked to a ref entity, one per artifact (an
        artifact linked to several refs of one dataset appears once)."""
        query = _artifacts_by_ref_query(kind, link, by_derived)
        documents = (await self._read_batch([(query, [{"v": value}])]))[0]
        unique: dict = {}
        for document in documents:
            identity = document["id"] if kind == "node" else self._edge_identity_of(document)
            unique.setdefault(identity, document)
        return unique

    @staticmethod
    def _edge_identity_of(document: dict) -> EdgeIdentity:
        return EdgeIdentity(document["source"], document["target"], document["relationship_name"])

    async def find_nodes_by_source_ref(self, source_ref_key: str) -> list[str]:
        return list(await self._artifacts_by_ref("node", "ref", False, source_ref_key))

    async def find_edges_by_source_ref(self, source_ref_key: str) -> list[EdgeIdentity]:
        return list(await self._artifacts_by_ref("edge", "ref", False, source_ref_key))

    def _keys_owned_by_dataset(self, document: dict, dataset_id: str) -> list[str]:
        keys, _refs = self._decode_provenance(document)
        return [key for key in keys if str(get_dataset_id_from_source_ref_key(key)) == dataset_id]

    def _keys_contributed_by_run(self, document: dict, pipeline_run_id: str) -> list[str]:
        _keys, run_refs = self._decode_provenance(document)
        return [
            get_source_ref_key_from_source_run_ref(ref)
            for ref in run_refs
            if str(get_pipeline_run_id_from_source_run_ref(ref)) == pipeline_run_id
        ]

    async def find_node_source_refs_by_dataset(self, dataset_id: str) -> dict[str, list[str]]:
        found = await self._artifacts_by_ref("node", "ref", True, dataset_id)
        result = {}
        for identity, doc in found.items():
            owned = self._keys_owned_by_dataset(doc, dataset_id)
            if owned:
                result[identity] = owned
        return result

    async def find_edge_source_refs_by_dataset(
        self, dataset_id: str
    ) -> dict[EdgeIdentity, list[str]]:
        found = await self._artifacts_by_ref("edge", "ref", True, dataset_id)
        result = {}
        for identity, doc in found.items():
            owned = self._keys_owned_by_dataset(doc, dataset_id)
            if owned:
                result[identity] = owned
        return result

    async def find_node_source_refs_by_pipeline_run(
        self, pipeline_run_id: str
    ) -> dict[str, list[str]]:
        found = await self._artifacts_by_ref("node", "run", True, pipeline_run_id)
        result = {}
        for identity, doc in found.items():
            contributed = self._keys_contributed_by_run(doc, pipeline_run_id)
            if contributed:
                result[identity] = contributed
        return result

    async def find_edge_source_refs_by_pipeline_run(
        self, pipeline_run_id: str
    ) -> dict[EdgeIdentity, list[str]]:
        found = await self._artifacts_by_ref("edge", "run", True, pipeline_run_id)
        result = {}
        for identity, doc in found.items():
            contributed = self._keys_contributed_by_run(doc, pipeline_run_id)
            if contributed:
                result[identity] = contributed
        return result

    async def set_graph_metadata(self, metadata: dict[str, str]) -> None:
        if not metadata:
            return
        rows = [{"k": str(key), "v": str(value)} for key, value in metadata.items()]
        await self._write_batch([(_METADATA_SET, rows)])

    async def get_graph_metadata(self) -> dict[str, str]:
        return {doc["k"]: doc["v"] for doc in (await self._read_batch([_METADATA_GET]))[0]}
