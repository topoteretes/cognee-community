"""TypeDB graph database adapter for cognee.

Cognee's property graph is stored in the reified schema in ``schema.tql``:
one ``node`` entity type and one ``edge`` relation type (roles ``source`` /
``target``). Node labels and relationship names are attributes; the full
property payload is the ``properties-json`` attribute, which is the canonical
record, and the promoted ``node-type`` / ``name`` attributes mirror it as
query accelerators. An edge's identity is its ``edge-key`` (a JSON
``[source, target, relationship]`` triple); ``edge-object-id`` is cognee's
feedback id. Timestamps are epoch milliseconds: a node's ``created-at``
mirrors its payload, an edge's is set on first write, ``updated-at`` is the
write time. Feedback weights and truth state live inside ``properties-json``.
Provenance is in ``provenance.py``, the TypeQL in ``queries.py``.

The TypeDB Python driver is synchronous, so driver work runs on a small
dedicated thread pool behind cognee's async interface. Batch writes run as
chunked transactions of ``WRITE_CHUNK_ROWS`` rows with ``WRITE_CONCURRENCY``
in flight, so a batch is not atomic (as with the sibling adapters). Commit
isolation conflicts are retried on every write path; a failed commit rolls
the whole transaction back, so a retry never duplicates work.
"""

import asyncio
import contextlib
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from typing import Any
from uuid import UUID

from cognee.exceptions import CogneeValidationError
from cognee.infrastructure.databases.graph.graph_db_interface import GraphDBInterface
from cognee.infrastructure.engine import DataPoint
from cognee.modules.engine.utils import generate_edge_object_id
from cognee.modules.retrieval.exceptions import SearchTypeNotSupported
from cognee.modules.storage.utils import JSONEncoder
from cognee.shared.logging_utils import get_logger

from .provenance import ProvenanceMixin
from .queries import (
    _ALL_EDGE_ENDPOINTS,
    _ALL_EDGES,
    _ALL_NODE_IDS,
    _ALL_NODES,
    _COMMENT_RE,
    _DELETE_INCIDENT_EDGES,
    _DELETE_LINKS_OF_INCIDENT_EDGES,
    _DELETE_LINKS_OF_LABELED_EDGES,
    _DELETE_LINKS_OF_NODES,
    _DELETE_NODES,
    _EDGE_UPSERT,
    _EDGES_BY_OBJECT_ID,
    _FETCH_NODES,
    _HAS_EDGES,
    _INCIDENT_EDGES_IN,
    _INCIDENT_EDGES_OUT,
    _ISOLATED_NODE_IDS,
    _NEIGHBOURS,
    _NODE_UPSERT,
    _PRE_LINK_PROVENANCE_ATTRIBUTES,
    _PROMOTED_FILTER_ATTRS,
    _REMOVE_LABELED_EDGES,
    _SCHEMA_KEYWORDS,
    _SET_EDGE_CREATED_AT,
    _STRING_LITERAL_RE,
    _TRIPLETS_BATCH,
    _WRITE_STAGE_RE,
    COGNEE_SCHEMA,
    _edge_key,
    _link_deletes,
    _now_ms,
    _properties_read_query,
    _properties_write_query,
)

logger = get_logger("TypeDBAdapter")

DEFAULT_ADDRESS = "127.0.0.1:1729"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "password"
DEFAULT_DATABASE = "cognee"
# TLS is off by default (local/docker servers); TypeDB Cloud and hardened
# deployments need it. Read from the environment because cognee's graph config
# has no provider-specific fields.
TLS_ENV = "TYPEDB_TLS"
TLS_ROOT_CA_ENV = "TYPEDB_TLS_ROOT_CA"

# Batch writes are split into transactions of this many rows, with up to
# WRITE_CONCURRENCY transactions in flight (measured defaults: per-row cost
# grows superlinearly with rows per transaction; four in flight is the knee).
# Defaults; per-adapter values come from TYPEDB_WRITE_CHUNK_ROWS /
# TYPEDB_WRITE_CONCURRENCY when set (read at construction).
WRITE_CHUNK_ROWS = 100
WRITE_CONCURRENCY = 4
# Commit conflicts ([STC2]) are retried for this long with capped, fully
# jittered backoff: a time budget, because a writer contending with another
# process on the same database can lose every round until the other batch ends.
COMMIT_RETRY_SECONDS = 30.0
COMMIT_BACKOFF_CAP_SECONDS = 0.25
COMMIT_RETRY_WARN_AFTER = 10
WRITE_CHUNK_ROWS_ENV = "TYPEDB_WRITE_CHUNK_ROWS"
WRITE_CONCURRENCY_ENV = "TYPEDB_WRITE_CONCURRENCY"


def _get_env_variable_as_positive_int(name: str, default: int, environ=os.environ) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name}={raw!r} is not an integer") from error
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value


class TypeDBAdapter(ProvenanceMixin, GraphDBInterface):
    """Adapter for TypeDB as a cognee graph store."""

    # Cognee gates its Cypher-generating search types on this flag; TypeQL-only
    # adapters must opt out so those searches fail with SearchTypeNotSupported
    # instead of a TypeQL parse error.
    supports_cypher_queries: bool = False

    def __init__(
        self,
        graph_database_url: str | None = None,
        graph_database_username: str | None = None,
        graph_database_password: str | None = None,
        graph_database_port: int | None = None,
        graph_database_key: str | None = None,
        database_name: str | None = None,
        **kwargs,
    ):
        # Cognee configs commonly carry scheme-prefixed URLs (any scheme —
        # the field is shared across graph providers); TypeDB wants host:port.
        address = (graph_database_url or DEFAULT_ADDRESS).split("://", 1)[-1]
        if graph_database_port and ":" not in address:
            address = f"{address}:{graph_database_port}"

        self.address = address
        self.username = graph_database_username or DEFAULT_USERNAME
        self.password = graph_database_password or DEFAULT_PASSWORD
        self.database_name = database_name or DEFAULT_DATABASE

        self._driver = None
        # _database_exists: a read has seen the database on the server.
        # _schema_initialized: a write has run the (idempotent) schema define.
        # Reads never provision — a stale engine handle used after the
        # database was dropped must see an empty graph, not recreate it.
        self._database_exists = False
        self._schema_initialized = False
        self._lock = asyncio.Lock()
        self._chunk_rows = _get_env_variable_as_positive_int(WRITE_CHUNK_ROWS_ENV, WRITE_CHUNK_ROWS)
        self._write_concurrency = _get_env_variable_as_positive_int(
            WRITE_CONCURRENCY_ENV, WRITE_CONCURRENCY
        )
        self._commit_retry_seconds = COMMIT_RETRY_SECONDS
        # Caps in-flight chunk transactions across ALL concurrent batch calls.
        self._write_semaphore = asyncio.Semaphore(self._write_concurrency)
        # Guards driver open/close and executor creation across threads.
        self._state_lock = threading.Lock()
        # Set while close() drains the pool: no new driver or pool may open,
        # so a queued worker cannot leave a fresh driver behind.
        self._closing = False
        # Small dedicated pool: makes the concurrency ceiling on the shared
        # native driver explicit instead of borrowing the default executor.
        self._executor: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------
    # Connection plumbing (synchronous; always called from worker threads)
    # ------------------------------------------------------------------

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._state_lock:
            if self._executor is None:
                if self._closing:
                    raise RuntimeError("TypeDB adapter is closing")
                # One thread beyond the write concurrency so a read is never
                # queued behind a full set of in-flight write chunks.
                self._executor = ThreadPoolExecutor(
                    max_workers=self._write_concurrency + 1, thread_name_prefix="typedb-adapter"
                )
            return self._executor

    async def _run_sync(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._get_executor(), fn, *args)

    @staticmethod
    def _tls_config_from_env(environ=os.environ):
        """TLS settings from ``TYPEDB_TLS`` (unset/false: plaintext; true: the
        system trust roots, or the PEM bundle at ``TYPEDB_TLS_ROOT_CA``).
        Any other value is an error rather than a silent fall back."""
        from typedb.driver import DriverTlsConfig

        value = environ.get(TLS_ENV, "").strip().lower()
        if value in {"", "0", "false", "no", "off"}:
            return DriverTlsConfig.disabled()
        if value not in {"1", "true", "yes", "on"}:
            raise ValueError(f"{TLS_ENV}={value!r} is not a boolean (use true or false)")
        root_ca = environ.get(TLS_ROOT_CA_ENV, "").strip()
        if root_ca:
            return DriverTlsConfig.enabled_with_root_ca(root_ca)
        return DriverTlsConfig.enabled_with_native_root_ca()

    def _get_driver(self):
        """Lazily open the (synchronous) TypeDB driver. While the adapter is
        closing, work already queued may still use the open driver, but no
        new one is created."""
        with self._state_lock:
            if self._driver is None:
                if self._closing:
                    raise RuntimeError("TypeDB adapter is closing")
                from typedb.driver import Credentials, DriverOptions, TypeDB

                self._driver = TypeDB.driver(
                    self.address,
                    Credentials(self.username, self.password),
                    DriverOptions(self._tls_config_from_env()),
                )
            return self._driver

    def _begin_closing(self):
        """Mark the adapter closing and take the pool to drain. Queued workers
        keep the open driver; none can open a new driver or pool."""
        with self._state_lock:
            self._closing = True
            executor, self._executor = self._executor, None
        return executor

    def _finish_closing(self):
        """After the pool has drained: take the driver to close and reset to
        the unopened state, so the adapter reopens lazily if used again."""
        with self._state_lock:
            driver, self._driver = self._driver, None
            self._database_exists = False
            self._schema_initialized = False
            self._closing = False
        return driver

    def _close_sync(self) -> None:
        executor = self._begin_closing()
        try:
            if executor is not None:
                executor.shutdown(wait=True)
        finally:
            driver = self._finish_closing()
        if driver is not None:
            driver.close()

    async def close(self) -> None:
        """Release the worker threads and the driver (cognee calls this on
        cache eviction). The pool drains before the driver closes, so
        in-flight transactions finish and nothing opens a driver afterwards."""
        async with self._lock:
            executor = self._begin_closing()
            try:
                if executor is not None:
                    await asyncio.to_thread(executor.shutdown, True)
            finally:
                driver = self._finish_closing()
            if driver is not None:
                await asyncio.to_thread(driver.close)

    def _provision_database_sync(self) -> None:
        """Create the database if missing and (re)define the schema. Write
        path only; the define is idempotent and always runs, so additive
        schema changes reach existing databases."""
        from typedb.driver import TransactionType

        driver = self._get_driver()
        if not driver.databases.contains(self.database_name):
            try:
                driver.databases.create(self.database_name)
            except Exception:
                # Lost a race with another creator (cognee's dataset lock is
                # per process; two workers can provision the same dataset).
                # The define below is idempotent, so proceed if it now exists.
                if not driver.databases.contains(self.database_name):
                    raise
        else:
            self._refuse_pre_link_schema_sync(driver)
        with driver.transaction(self.database_name, TransactionType.SCHEMA) as tx:
            tx.query(COGNEE_SCHEMA).resolve()
            tx.commit()
        self._database_exists = True
        self._schema_initialized = True

    def _refuse_pre_link_schema_sync(self, driver) -> None:
        """Refuse a database written by an earlier version that stored
        provenance as artifact attributes: the additive define would succeed
        and every artifact would then read as unowned."""
        from typedb.driver import TransactionType

        # Phrased over all ownerships so it also runs on a database that has
        # no `node` type yet (an unknown label would be a query error).
        with driver.transaction(self.database_name, TransactionType.READ) as tx:
            owned = {
                row.get("a").get_label()
                for row in tx.query("match $t owns $a; select $t, $a;").resolve()
                if row.get("t").get_label() == "node"
            }
        if owned & _PRE_LINK_PROVENANCE_ATTRIBUTES:
            raise RuntimeError(
                f"TypeDB database {self.database_name!r} was created by an earlier version "
                "of this adapter (provenance stored as artifact attributes). Its provenance "
                "cannot be read by the relational model; create a fresh database."
            )

    async def _provision_database(self) -> None:
        if self._schema_initialized:
            return
        async with self._lock:
            if not self._schema_initialized:
                await self._run_sync(self._provision_database_sync)

    def _database_exists_sync(self) -> bool:
        exists = self._get_driver().databases.contains(self.database_name)
        self._database_exists = exists
        return exists

    async def _database_available(self) -> bool:
        """Read-path gate: True if the database exists; never creates it."""
        if self._schema_initialized or self._database_exists:
            return True
        return await self._run_sync(self._database_exists_sync)

    def _transaction_type_for(self, query_text: str):
        from typedb.driver import TransactionType

        bare = _COMMENT_RE.sub(" ", _STRING_LITERAL_RE.sub(" ", query_text))
        first_word = bare.lstrip().split(None, 1)[0].lower() if bare.strip() else ""
        if first_word in _SCHEMA_KEYWORDS:
            return TransactionType.SCHEMA
        if _WRITE_STAGE_RE.search(bare):
            return TransactionType.WRITE
        return TransactionType.READ

    def _run_batch_sync(self, specs, transaction_type, collect_rows: bool):
        """Run (query, given_rows) specs in order in one transaction. Promises
        are all fired before any is resolved (pipelined, order preserved).
        Write answers are never iterated: a later write interrupts earlier
        answer streams (TSV13), and resolve() still surfaces errors."""
        from typedb.driver import TransactionType

        driver = self._get_driver()
        results = []
        with driver.transaction(self.database_name, transaction_type) as tx:
            promises = [
                (query_text, tx.query(query_text, given_rows=given_rows))
                for query_text, given_rows in specs
            ]
            for query_text, promise in promises:
                try:
                    answer = promise.resolve()
                    results.append(self._collect_answer(answer) if collect_rows else [])
                except Exception:
                    logger.error(
                        "TypeDB query failed (%s tx): %.300s", transaction_type, query_text
                    )
                    raise
            if transaction_type != TransactionType.READ:
                tx.commit()
        return results

    @staticmethod
    def _normalize_query_specs(queries) -> list[tuple[str, list | None]]:
        return [(query, None) if isinstance(query, str) else query for query in queries]

    async def _read_batch(self, queries) -> list[list[dict]]:
        """Run read queries in one READ transaction; rows per query. A missing
        database yields empty results rather than being created."""
        from typedb.driver import TransactionType

        if not await self._database_available():
            return [[] for _ in queries]
        return await self._run_sync(
            self._run_batch_sync, self._normalize_query_specs(queries), TransactionType.READ, True
        )

    @staticmethod
    def _is_commit_conflict(error: Exception) -> bool:
        """TypeDB [STC2]: commit lost an isolation conflict; nothing was committed."""
        return str(error).lstrip().startswith("[STC2]")

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # Full jitter, exponential up to the cap: two losers never lock-step.
        await asyncio.sleep(random.random() * min(COMMIT_BACKOFF_CAP_SECONDS, 0.02 * 2**attempt))

    async def _retry_commit_conflicts(self, attempt, retry: bool = True):
        """Run ``attempt()`` until it succeeds or the conflict budget ends.
        Only [STC2] commit conflicts are retried; any other error propagates.
        A warning names the contention after COMMIT_RETRY_WARN_AFTER rounds."""
        deadline = time.monotonic() + self._commit_retry_seconds
        rounds = 0
        while True:
            try:
                return await attempt()
            except Exception as error:
                if not retry or not self._is_commit_conflict(error):
                    raise
                if time.monotonic() >= deadline:
                    raise
                rounds += 1
                if rounds == COMMIT_RETRY_WARN_AFTER:
                    logger.warning(
                        "TypeDB commit conflicts on %s: %d retries so far, another writer "
                        "is active on this database (budget %.0fs)",
                        self.database_name,
                        rounds,
                        self._commit_retry_seconds,
                    )
                await self._backoff(rounds)

    async def _run_sync_shielded(self, fn, *args):
        """``_run_sync`` for write transactions: a cancelled caller waits for
        the in-flight transaction to finish (commit or fail) before the
        cancellation propagates, so no chunk can commit after cognee's
        rollback has already looked."""
        task = asyncio.ensure_future(self._run_sync(fn, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await asyncio.shield(task)
            raise

    async def _write_batch(self, queries, retry: bool = True) -> None:
        """Run write queries in one WRITE transaction, retried on commit
        conflicts; answers are discarded (see _run_batch_sync)."""
        from typedb.driver import TransactionType

        await self._provision_database()
        specs = self._normalize_query_specs(queries)

        async def attempt():
            await self._run_sync_shielded(self._run_batch_sync, specs, TransactionType.WRITE, False)

        await self._retry_commit_conflicts(attempt, retry)

    async def _write_rows(
        self,
        template: str,
        rows: list[dict],
        created_query: str | None = None,
        key: str | None = None,
        provenance=None,
    ):
        """Upsert rows as chunked transactions, WRITE_CONCURRENCY in flight.

        ``created_query`` (with ``key``) adds the set-once created-at statement
        to each chunk. ``provenance`` = (kind, id_field, attach) folds the
        provenance attach into each chunk's transaction, after the batch's
        ref entities are put in their own transaction. On the first failure,
        chunks not yet started are cancelled and committed chunks stay; an
        in-flight chunk runs to its end before the failure or a cancellation
        propagates.
        """
        if not rows:
            return
        size = self._chunk_rows
        chunks = [rows[i : i + size] for i in range(0, len(rows), size)]

        def specs_for(chunk):
            specs = [(template, chunk)]
            if created_query is not None:
                specs.append((created_query, [{key: row[key], "now": row["now"]} for row in chunk]))
            return specs

        def provenance_for(chunk):
            if provenance is None:
                return None
            kind, id_field, attach = provenance
            return (kind, [row[id_field] for row in chunk], attach.transition)

        if provenance is not None:
            await self._ensure_provenance_refs(provenance[2])

        tasks = [
            asyncio.create_task(self._write_chunk(specs_for(chunk), provenance_for(chunk)))
            for chunk in chunks
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _write_chunk(self, specs, provenance=None) -> None:
        """One chunk transaction, retried on commit conflicts. With
        ``provenance`` = (kind, identities, transition) the transaction also
        applies the provenance transition to those artifacts after the specs."""

        async def attempt():
            # The slot is held only for the attempt, never during backoff.
            async with self._write_semaphore:
                if provenance is None:
                    await self._write_batch(specs, retry=False)
                else:
                    kind, identities, transition = provenance
                    await self._run_sync_shielded(
                        self._provenance_change_sync, kind, identities, transition, specs
                    )

        await self._retry_commit_conflicts(attempt)

    def _mutate_properties_sync(self, kind: str, identities, mutate) -> set[str]:
        """In one WRITE transaction: read properties-json for ``identities``
        (all artifacts when None), apply ``mutate(identity, props) -> props |
        None``, write back the changed payloads, commit. Returns updated ids."""
        from typedb.driver import TransactionType

        driver = self._get_driver()
        with driver.transaction(self.database_name, TransactionType.WRITE) as tx:
            if identities is None:
                answer = tx.query(_properties_read_query(kind, True)).resolve()
            else:
                rows = [{"id": identity} for identity in dict.fromkeys(identities)]
                answer = tx.query(_properties_read_query(kind, False), given_rows=rows).resolve()
            documents = self._collect_answer(answer)
            now = _now_ms()
            updates = []
            for document in documents:
                try:
                    properties = json.loads(document["p"]) if document.get("p") else {}
                except (TypeError, ValueError):
                    continue
                changed = mutate(document["id"], properties)
                if changed is not None:
                    updates.append(
                        {
                            "id": document["id"],
                            "v": json.dumps(changed, cls=JSONEncoder),
                            "now": now,
                        }
                    )
            if updates:
                tx.query(_properties_write_query(kind), given_rows=updates).resolve()
            tx.commit()
        return {update["id"] for update in updates}

    async def _mutate_properties(self, kind: str, identities, mutate) -> set[str]:
        """Read-modify-write ``properties-json`` through ``mutate``.
        ``identities=None`` finds the candidates in a READ transaction first,
        so the write transaction touches only the rows it changes."""
        if identities is None:
            documents = (await self._read_batch([_properties_read_query(kind, True)]))[0]
            identities = []
            for document in documents:
                try:
                    properties = json.loads(document["p"]) if document.get("p") else {}
                except (TypeError, ValueError):
                    continue
                if mutate(document["id"], properties) is not None:
                    identities.append(document["id"])
        if not identities:
            return set()
        await self._provision_database()
        return await self._retry_commit_conflicts(
            lambda: self._run_sync_shielded(self._mutate_properties_sync, kind, identities, mutate)
        )

    @classmethod
    def _concept_to_value(cls, concept) -> Any:
        if concept is None:
            return None
        value = concept.try_get_value()
        if value is not None:
            return value
        if concept.is_type():
            return concept.get_label()
        return concept.try_get_iid()

    @classmethod
    def _collect_answer(cls, answer) -> list[dict[str, Any]]:
        """Convert a QueryAnswer into a list of plain dicts.

        Fetch queries yield JSON documents; concept rows are decoded to raw
        attribute/value payloads (types to labels, other instances to IIDs).
        """
        if answer.is_concept_documents():
            return list(answer.as_concept_documents())
        if answer.is_concept_rows():
            return [
                {name: cls._concept_to_value(row.get(name)) for name in row.column_names()}
                for row in answer.as_concept_rows()
            ]
        return []

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _document_to_node_dict(document: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the cognee node property dict from a fetched `{ $n.* }` doc."""
        properties = {}
        raw = document.get("properties-json")
        if raw:
            try:
                properties = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("Undecodable properties-json for node %s", document.get("node-id"))
        properties.setdefault("id", document.get("node-id"))
        for attribute, key in (("created-at", "created_at"), ("updated-at", "updated_at")):
            if document.get(attribute) is not None:
                properties.setdefault(key, document[attribute])
        return properties

    @staticmethod
    def _document_to_edge_properties(document: dict[str, Any]) -> dict[str, Any]:
        raw = document.get("properties-json")
        if raw:
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("Undecodable properties-json for an edge")
        return {}

    @staticmethod
    def _node_upsert_row_from_properties(
        node_id: str, properties: dict[str, Any], fallback_type: str
    ) -> dict[str, Any]:
        """The shared given-row shape for a node upsert (no provenance keys)."""
        name = properties.get("name")
        created = properties.get("created_at")
        return {
            "id": node_id,
            "type": str(properties.get("type") or fallback_type),
            "name": str(name) if name is not None else "",
            "props": json.dumps(properties, cls=JSONEncoder),
            # Mirrors DataPoint.created_at (epoch ms). Payloads without one
            # (add_node's dict form) get the write time.
            "created": (
                created if isinstance(created, int) and not isinstance(created, bool) else _now_ms()
            ),
        }

    def _node_upsert_row(self, node: DataPoint) -> dict[str, Any]:
        return self._node_upsert_row_from_properties(
            str(node.id), node.model_dump(), type(node).__name__
        )

    def _neighbour_query_spec(self, node_id: str, incoming: bool, edge_label: str | None = None):
        anchor_role, neighbour_role = ("target", "source") if incoming else ("source", "target")
        query = _NEIGHBOURS.format(
            label_decl=", $label: string" if edge_label is not None else "",
            anchor_role=anchor_role,
            neighbour_role=neighbour_role,
            label_constraint=", has relationship-name == $label" if edge_label is not None else "",
        )
        row: dict[str, Any] = {"id": str(node_id)}
        if edge_label is not None:
            row["label"] = edge_label
        return (query, [row])

    # ------------------------------------------------------------------
    # GraphDBInterface — cognee 1.6.1 call surface
    # ------------------------------------------------------------------

    async def query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        transaction_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a raw TypeQL query.

        ``params`` are forwarded as one ``given`` row, so a parameterized
        query declares a matching ``given`` stage, e.g.::

            query('given $name: string; match $n isa node, has name == $name; '
                  'fetch { "node": { $n.* } };', {"name": "cognee"})

        ``transaction_type`` ("read" | "write" | "schema") overrides the
        keyword-based inference.
        """
        from typedb.driver import TransactionType

        given_rows = [params] if params else None
        resolved = (
            TransactionType[transaction_type.upper()]
            if transaction_type is not None
            else self._transaction_type_for(query)
        )
        if resolved == TransactionType.READ:
            if not await self._database_available():
                return []
        else:
            await self._provision_database()
        specs = [(query, given_rows)]

        async def attempt():
            return (await self._run_sync(self._run_batch_sync, specs, resolved, True))[0]

        return await self._retry_commit_conflicts(attempt, resolved != TransactionType.READ)

    async def has_node(self, node_id: str) -> bool:
        results = await self._read_batch([(_FETCH_NODES, [{"id": str(node_id)}])])
        return bool(results[0])

    async def add_node(self, node: DataPoint | str, properties: dict[str, Any] | None = None):
        """Add (or update) one node from a DataPoint or an id + properties.
        Carries no provenance; existing links are left untouched."""
        if isinstance(node, DataPoint):
            row = self._node_upsert_row(node)
        else:
            node_props = dict(properties or {})
            node_props.setdefault("id", str(node))
            row = self._node_upsert_row_from_properties(str(node), node_props, "node")
        row["now"] = _now_ms()
        await self._write_batch([(_NODE_UPSERT, [row])])

    async def add_nodes(
        self,
        nodes: list[DataPoint],
        source_ref_key: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        """Upsert a batch of DataPoints in chunked, concurrent transactions,
        attaching provenance per chunk when ``source_ref_key`` is given. Rows
        sharing a node id collapse to the last one."""
        if not nodes:
            return
        attach = self._attach_for_batch(source_ref_key, pipeline_run_id)
        now = _now_ms()
        rows: dict[str, dict[str, Any]] = {}
        for node in nodes:
            row = self._node_upsert_row(node)
            row["now"] = now
            rows[row["id"]] = row
        await self._write_rows(
            _NODE_UPSERT,
            list(rows.values()),
            provenance=("node", "id", attach) if attach else None,
        )

    async def extract_node(self, node_id: str):
        return await self.get_node(node_id)

    async def extract_nodes(self, node_ids: list[str]):
        return await self.get_nodes(node_ids)

    async def delete_node(self, node_id: str):
        await self.delete_nodes([node_id])

    async def delete_nodes(self, node_ids: list[str]) -> None:
        """Delete nodes and their incident edges in one transaction."""
        if not node_ids:
            return
        rows = [{"id": node_id} for node_id in dict.fromkeys(str(i) for i in node_ids)]
        # Provenance links and edges first: deleting a player leaves a
        # dangling relation, whether that relation is an edge or a link.
        link_queries = _link_deletes(_DELETE_LINKS_OF_INCIDENT_EDGES) + _link_deletes(
            _DELETE_LINKS_OF_NODES
        )
        await self._write_batch(
            [(query, rows) for query in link_queries]
            + [(_DELETE_INCIDENT_EDGES, rows), (_DELETE_NODES, rows)]
        )

    async def has_edge(self, source_id, target_id, relationship_name: str) -> bool:
        matched = await self.has_edges([(source_id, target_id, relationship_name)])
        return bool(matched)

    async def has_edges(self, edges):
        """The (source_id, target_id, relationship_name) tuples that exist;
        cognee consumes a list of tuples here, not booleans."""
        if not edges:
            return []
        rows = [
            {
                "key": _edge_key(str(edge[0]), str(edge[1]), edge[2]),
                "sid": str(edge[0]),
                "tid": str(edge[1]),
                "rel": edge[2],
            }
            for edge in edges
        ]
        results = await self._read_batch([(_HAS_EDGES, rows)])
        # De-duplicate while preserving first-seen order.
        return list(
            dict.fromkeys(
                (doc["source"], doc["target"], doc["relationship_name"]) for doc in results[0]
            )
        )

    async def add_edge(
        self,
        source_id,
        target_id,
        relationship_name: str,
        properties: dict[str, Any] | None = None,
    ):
        await self.add_edges([(str(source_id), str(target_id), relationship_name, properties)])

    async def add_edges(
        self,
        edges: list[tuple[str, str, str, dict[str, Any]]],
        source_ref_key: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        """Upsert a batch of edges in chunked, concurrent transactions,
        attaching provenance per chunk when ``source_ref_key`` is given.
        Identity is the edge-key: properties are replaced on re-add and
        duplicates within a batch collapse to the last row. Edges whose
        endpoints are missing are skipped (cognee adds nodes first)."""
        if not edges:
            return
        attach = self._attach_for_batch(source_ref_key, pipeline_run_id)
        now = _now_ms()
        rows: dict[str, dict[str, Any]] = {}
        for source_id, target_id, relationship_name, properties in edges:
            source_id, target_id = str(source_id), str(target_id)
            edge_properties = {
                **(properties or {}),
                "source_node_id": source_id,
                "target_node_id": target_id,
                "relationship_name": relationship_name,
            }
            key = _edge_key(source_id, target_id, relationship_name)
            # CogneeGraph reads edge_object_id from the projected properties,
            # so the derived id is written into the payload as well.
            edge_object_id = str(
                edge_properties.get("edge_object_id")
                or generate_edge_object_id(source_id, target_id, relationship_name)
            )
            edge_properties.setdefault("edge_object_id", edge_object_id)
            rows[key] = {
                "key": key,
                "sid": source_id,
                "tid": target_id,
                "rel": relationship_name,
                "eoid": edge_object_id,
                "props": json.dumps(edge_properties, cls=JSONEncoder),
                "now": now,
            }
        await self._write_rows(
            _EDGE_UPSERT,
            list(rows.values()),
            _SET_EDGE_CREATED_AT,
            "key",
            provenance=("edge", "key", attach) if attach else None,
        )

    async def get_edges(self, node_id: str):
        """Edges incident to a node as (node_id, neighbour_id, {...}): slot 0
        is always the queried node, whatever the edge's direction, which is
        what cognee's format_edges assumes."""
        anchor = str(node_id)
        seen: dict[tuple[str, str, str], None] = {}
        for document in await self._incident_edge_documents([anchor]):
            other = document["target"] if document["source"] == anchor else document["source"]
            seen[(anchor, other, document["relationship_name"])] = None
        return [
            (first, second, {"relationship_name": relationship})
            for first, second, relationship in seen
        ]

    async def get_predecessors(self, node_id: str, edge_label: str | None = None) -> list:
        results = await self._read_batch([self._neighbour_query_spec(node_id, True, edge_label)])
        return [self._document_to_node_dict(doc["neighbour"]) for doc in results[0]]

    async def get_successors(self, node_id: str, edge_label: str | None = None) -> list:
        results = await self._read_batch([self._neighbour_query_spec(node_id, False, edge_label)])
        return [self._document_to_node_dict(doc["neighbour"]) for doc in results[0]]

    async def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        """Predecessors and successors combined, keyed on the node-id attribute
        (never the payload's "id", which callers may set independently)."""
        anchor = str(node_id)
        results = await self._read_batch(
            [
                self._neighbour_query_spec(anchor, True),
                self._neighbour_query_spec(anchor, False),
            ]
        )
        neighbours = [self._document_to_node_dict(doc["neighbour"]) for doc in results[0]]
        neighbours.extend(
            self._document_to_node_dict(doc["neighbour"])
            for doc in results[1]
            # A self-loop is already reported by the incoming pass.
            if doc["neighbour"].get("node-id") != anchor
        )
        return neighbours

    async def _incident_edge_documents(self, node_ids) -> list[dict[str, Any]]:
        """All edge documents incident to the given node ids (both directions)."""
        ids = sorted(set(node_ids))
        if not ids:
            return []
        rows = [{"id": i} for i in ids]
        outgoing, incoming = await self._read_batch(
            [(_INCIDENT_EDGES_OUT, rows), (_INCIDENT_EDGES_IN, rows)]
        )
        return outgoing + incoming

    @classmethod
    def _edges_within_node_set(
        cls, edge_docs, selected, wanted_types: set[str] | None = None
    ) -> list[tuple[str, str, str, dict]]:
        """Deduped (source, target, rel, props) with both endpoints in ``selected``."""
        edges: dict[tuple[str, str, str], dict] = {}
        for document in edge_docs:
            relationship = document["relationship_name"]
            if wanted_types is not None and relationship not in wanted_types:
                continue
            if document["source"] in selected and document["target"] in selected:
                key = (document["source"], document["target"], relationship)
                if key not in edges:
                    edges[key] = cls._document_to_edge_properties(document["edge"])
        return [(source, target, rel, props) for (source, target, rel), props in edges.items()]

    async def _fetch_with_incident_edges(self, node_ids) -> tuple[list[dict], list[dict]]:
        """Node documents for ``node_ids`` plus all their incident edge documents,
        in a single read transaction (fetch + both directional sweeps)."""
        ids = sorted({str(node_id) for node_id in node_ids})
        if not ids:
            return [], []
        rows = [{"id": node_id} for node_id in ids]
        node_docs, outgoing, incoming = await self._read_batch(
            [(_FETCH_NODES, rows), (_INCIDENT_EDGES_OUT, rows), (_INCIDENT_EDGES_IN, rows)]
        )
        return node_docs, outgoing + incoming

    @classmethod
    def _add_unknown_endpoints(
        cls, edge_docs, nodes: dict[str, dict], wanted_types: set[str] | None = None
    ) -> set[str]:
        """Add the not-yet-known endpoints of ``edge_docs`` to ``nodes``.

        Returns the ids added (the next BFS frontier). Edges outside
        ``wanted_types`` are not followed.
        """
        added: set[str] = set()
        for document in edge_docs:
            if wanted_types is not None and document["relationship_name"] not in wanted_types:
                continue
            for endpoint, node_doc in (
                (document["source"], document["source_node"]),
                (document["target"], document["target_node"]),
            ):
                if endpoint not in nodes:
                    nodes[endpoint] = cls._document_to_node_dict(node_doc)
                    added.add(endpoint)
        return added

    async def get_neighborhood(
        self,
        node_ids: list[str],
        depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> tuple[list[tuple[str, dict]], list[tuple[str, str, str, dict]]]:
        """K-hop neighborhood of the seed nodes, in get_graph_data() shape:
        the induced subgraph over the reached nodes, following only
        ``edge_types`` when given. Each node's incident edges cross the wire
        once."""
        if not node_ids:
            return ([], [])
        wanted_types = set(edge_types) if edge_types else None

        node_docs, edge_docs = await self._fetch_with_incident_edges(node_ids)
        nodes: dict[str, dict] = {
            doc["node"]["node-id"]: self._document_to_node_dict(doc["node"]) for doc in node_docs
        }
        swept = set(nodes)
        frontier = (
            self._add_unknown_endpoints(edge_docs, nodes, wanted_types) if depth > 0 else set()
        )
        for _ in range(1, max(depth, 0)):
            if not frontier:
                break
            documents = await self._incident_edge_documents(frontier)
            swept |= frontier
            edge_docs.extend(documents)
            frontier = self._add_unknown_endpoints(documents, nodes, wanted_types)

        # Edges between nodes of the final frontier were never swept.
        edge_docs.extend(await self._incident_edge_documents(set(nodes) - swept))
        return (
            list(nodes.items()),
            self._edges_within_node_set(edge_docs, set(nodes), wanted_types),
        )

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        nodes = await self.get_nodes([node_id])
        return nodes[0] if nodes else None

    async def get_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        rows = [{"id": str(node_id)} for node_id in node_ids]
        results = await self._read_batch([(_FETCH_NODES, rows)])
        return [self._document_to_node_dict(document["node"]) for document in results[0]]

    async def get_connections(self, node_id) -> list:
        """(source_node, {relationship_name}, target_node) triples for a node."""
        results = await self._read_batch(
            [
                self._neighbour_query_spec(str(node_id), True),
                self._neighbour_query_spec(str(node_id), False),
            ]
        )
        connections = []
        for document in results[0]:  # incoming: neighbour -> node
            connections.append(
                (
                    self._document_to_node_dict(document["neighbour"]),
                    {"relationship_name": document["relationship_name"]},
                    self._document_to_node_dict(document["node"]),
                )
            )
        anchor = str(node_id)
        for document in results[1]:  # outgoing: node -> neighbour
            if document["neighbour"].get("node-id") == anchor:
                continue  # self-loop already reported by the incoming pass
            connections.append(
                (
                    self._document_to_node_dict(document["node"]),
                    {"relationship_name": document["relationship_name"]},
                    self._document_to_node_dict(document["neighbour"]),
                )
            )
        return connections

    async def remove_connection_to_predecessors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        await self._remove_labeled_edges(node_ids, edge_label, incoming=True)

    async def remove_connection_to_successors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        await self._remove_labeled_edges(node_ids, edge_label, incoming=False)

    async def _remove_labeled_edges(
        self, node_ids: list[str], edge_label: str, incoming: bool
    ) -> None:
        if not node_ids:
            return
        anchor_role = "target" if incoming else "source"
        ids = dict.fromkeys(str(node_id) for node_id in node_ids)
        rows = [{"id": node_id, "label": edge_label} for node_id in ids]
        link_queries = _link_deletes(_DELETE_LINKS_OF_LABELED_EDGES, anchor_role=anchor_role)
        await self._write_batch(
            [(query, rows) for query in link_queries]
            + [(_REMOVE_LABELED_EDGES.format(anchor_role=anchor_role), rows)]
        )

    async def delete_graph(self):
        """Remove all nodes and edges (the schema is kept)."""
        await self._write_batch(
            [
                "match $l isa sourced-from; delete $l;",
                "match $l isa run-attached; delete $l;",
                "match $e isa edge; delete $e;",
                "match $n isa node; delete $n;",
                # Ref entities with no links left are swept too (a writer that
                # raced this and lost its refs fails its chunk with a clear
                # error rather than committing unowned artifacts).
                "match $r isa source-ref; not { $l isa sourced-from, links (ref: $r); };"
                " delete $r;",
                "match $r isa run-ref; not { $l isa run-attached, links (run: $r); }; delete $r;",
            ]
        )

    def serialize_properties(self, properties=None) -> dict[str, Any]:
        """Serialize property values so they round-trip through TypeDB.

        UUIDs become strings; nested dicts/lists become JSON strings (they are
        stored inside the `properties-json` attribute).
        """
        serialized = {}
        for key, value in (properties or {}).items():
            if isinstance(value, UUID):
                serialized[key] = str(value)
            elif isinstance(value, (dict, list)):
                serialized[key] = json.dumps(value, cls=JSONEncoder)
            else:
                serialized[key] = value
        return serialized

    # ------------------------------------------------------------------
    # Analytics tier
    # ------------------------------------------------------------------

    async def _get_all_graph_documents(self):
        return await self._read_batch([_ALL_NODES, _ALL_EDGES])

    async def get_model_independent_graph_data(self):
        """Nodes and (source, relationship, target) triples without model shaping."""
        node_docs, edge_docs = await self._get_all_graph_documents()
        nodes = [self._document_to_node_dict(document["node"]) for document in node_docs]
        elements = [
            [document["source"], document["relationship_name"], document["target"]]
            for document in edge_docs
        ]
        return ([{"nodes": nodes}], [{"elements": elements}])

    async def get_graph_data(self):
        """All nodes and edges, keyed by cognee node id (UUID string)."""
        node_docs, edge_docs = await self._get_all_graph_documents()
        nodes = [
            (document["node"]["node-id"], self._document_to_node_dict(document["node"]))
            for document in node_docs
        ]
        edges = [
            (
                document["source"],
                document["target"],
                document["relationship_name"],
                self._document_to_edge_properties(document["edge"]),
            )
            for document in edge_docs
        ]
        return (nodes, edges)

    async def get_id_filtered_graph_data(self, target_ids: list[str]):
        """Targets, their direct neighbours, and only the edges touching a
        target, in get_graph_data() shape. CogneeGraph prefers this to the
        whole-graph projection, which keeps search cost proportional to the
        search rather than to the graph."""
        if not target_ids:
            return ([], [])
        if not all(isinstance(target_id, str) for target_id in target_ids):
            raise CogneeValidationError("target_ids must be a list of strings")

        node_docs, edge_docs = await self._fetch_with_incident_edges(target_ids)
        nodes: dict[str, dict] = {
            doc["node"]["node-id"]: self._document_to_node_dict(doc["node"]) for doc in node_docs
        }
        if not nodes:
            return ([], [])
        self._add_unknown_endpoints(edge_docs, nodes)
        # Every swept edge touches a target, and both endpoints are now known.
        return (list(nodes.items()), self._edges_within_node_set(edge_docs, set(nodes)))

    async def get_nodeset_subgraph(
        self,
        node_type: type[Any],
        node_name: list[str],
        node_name_filter_operator: str = "OR",
    ) -> tuple[list[tuple[str, dict]], list[tuple[str, str, str, dict]]]:
        """Subgraph around nodes of ``node_type`` named in ``node_name``.

        "OR": seeds plus all their neighbours. "AND": seeds plus only the
        neighbours connected to every seed. Includes every edge whose two
        endpoints are in the selected set.
        """
        if not node_name:
            return ([], [])
        label = node_type.__name__
        seed_query = """
        given $label: string, $name: string;
        match $n isa node, has node-type == $label, has name == $name;
        fetch { "node": { $n.* } };
        """
        seed_rows = [{"label": label, "name": name} for name in node_name]
        seed_docs = (await self._read_batch([(seed_query, seed_rows)]))[0]
        seeds = {
            doc["node"]["node-id"]: self._document_to_node_dict(doc["node"]) for doc in seed_docs
        }
        if not seeds:
            return ([], [])

        seed_edge_docs = await self._incident_edge_documents(seeds)

        neighbour_seeds: dict[str, set[str]] = {}
        neighbour_docs: dict[str, dict] = {}
        for document in seed_edge_docs:
            for anchor, other, other_doc in (
                (document["source"], document["target"], document["target_node"]),
                (document["target"], document["source"], document["source_node"]),
            ):
                if anchor in seeds and other not in seeds:
                    neighbour_seeds.setdefault(other, set()).add(anchor)
                    neighbour_docs[other] = other_doc

        if node_name_filter_operator == "AND":
            wanted = {
                node_id for node_id, connected in neighbour_seeds.items() if connected == set(seeds)
            }
        else:
            wanted = set(neighbour_seeds)

        nodes = dict(seeds)
        nodes.update(
            {node_id: self._document_to_node_dict(neighbour_docs[node_id]) for node_id in wanted}
        )

        # Seed-incident edges are already swept; only the neighbours' own
        # edges (e.g. neighbour-to-neighbour) still need one sweep.
        edge_docs = seed_edge_docs + await self._incident_edge_documents(wanted)
        return (list(nodes.items()), self._edges_within_node_set(edge_docs, set(nodes)))

    async def get_filtered_graph_data(self, attribute_filters):
        """Nodes matching ``attribute_filters`` ({attribute: [values]}, all
        must match) and the edges between them. Filters on the promoted
        "type" / "name" attributes run server-side; others scan the payload
        client-side."""
        filters = {attribute: list(values) for attribute, values in attribute_filters[0].items()}
        promoted = (
            filters
            and all(attribute in _PROMOTED_FILTER_ATTRS for attribute in filters)
            and all(isinstance(value, str) for values in filters.values() for value in values)
        )

        if promoted:
            nodes_by_id = await self._filtered_nodes_server_side(filters)
            edge_docs = await self._incident_edge_documents(nodes_by_id)
            return (
                list(nodes_by_id.items()),
                self._edges_within_node_set(edge_docs, set(nodes_by_id)),
            )

        all_nodes, all_edges = await self.get_graph_data()
        # Membership on lists compares by equality, so unhashable property
        # values (lists/dicts from the JSON payload) never raise.
        nodes = [
            (node_id, properties)
            for node_id, properties in all_nodes
            if all(properties.get(attribute) in values for attribute, values in filters.items())
        ]
        selected = {node_id for node_id, _ in nodes}
        edges = [edge for edge in all_edges if edge[0] in selected and edge[1] in selected]
        return (nodes, edges)

    async def _filtered_nodes_server_side(self, filters: dict[str, list]) -> dict[str, dict]:
        attributes = sorted(filters)
        given = ", ".join(f"$v{index}: string" for index in range(len(attributes)))
        constraints = "".join(
            f", has {_PROMOTED_FILTER_ATTRS[attribute]} == $v{index}"
            for index, attribute in enumerate(attributes)
        )
        query = f'given {given};\nmatch $n isa node{constraints};\nfetch {{ "node": {{ $n.* }} }};'
        rows = [
            {f"v{index}": value for index, value in enumerate(combination)}
            for combination in product(*(filters[attribute] for attribute in attributes))
        ]
        documents = (await self._read_batch([(query, rows)]))[0]
        return {
            doc["node"]["node-id"]: self._document_to_node_dict(doc["node"]) for doc in documents
        }

    async def _edge_endpoint_pairs(self) -> tuple[list[str], list[tuple[str, str]]]:
        id_rows, endpoint_rows = await self._read_batch([_ALL_NODE_IDS, _ALL_EDGE_ENDPOINTS])
        node_ids = [row["id"] for row in id_rows]
        endpoints = [(row["sid"], row["tid"]) for row in endpoint_rows]
        return node_ids, endpoints

    @staticmethod
    def _connected_components(node_ids: list[str], endpoints: list[tuple[str, str]]):
        """Union-find over the edge list; returns {root: [member ids]}."""
        parent = {node_id: node_id for node_id in node_ids}

        def find(node_id: str) -> str:
            while parent[node_id] != node_id:
                parent[node_id] = parent[parent[node_id]]
                node_id = parent[node_id]
            return node_id

        for source, target in endpoints:
            if source in parent and target in parent:
                source_root, target_root = find(source), find(target)
                if source_root != target_root:
                    parent[target_root] = source_root

        components: dict[str, list[str]] = {}
        for node_id in parent:
            components.setdefault(find(node_id), []).append(node_id)
        return components

    async def get_disconnected_nodes(self) -> list[str]:
        """Ids of nodes with no incident edges at all (Ladybug's degree-zero
        semantics: cognee deletes every id returned here)."""
        rows = (await self._read_batch([_ISOLATED_NODE_IDS]))[0]
        return [row["id"] for row in rows]

    async def get_graph_metrics(self, include_optional=False):
        """Structural metrics; all-pairs metrics are reported unsupported (-1).
        Failures propagate rather than returning zeros cognee would persist."""
        try:
            node_ids, endpoints = await self._edge_endpoint_pairs()
            num_nodes = len(node_ids)
            num_edges = len(endpoints)
            components = self._connected_components(node_ids, endpoints)
            component_sizes = [len(members) for members in components.values()]

            return {
                "num_nodes": num_nodes,
                "num_edges": num_edges,
                "mean_degree": (2 * num_edges) / num_nodes if num_nodes > 0 else 0,
                "edge_density": num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0,
                "num_connected_components": len(component_sizes),
                "sizes_of_connected_components": component_sizes,
                "num_selfloops": (
                    sum(1 for source, target in endpoints if source == target)
                    if include_optional
                    else -1
                ),
                # All-pairs metrics need every shortest path, prohibitive on a
                # general graph; unsupported (-1) like the sibling adapters.
                "diameter": -1,
                "avg_shortest_path_length": -1,
                "avg_clustering": -1,
            }
        except Exception as error:
            logger.error("Failed to get graph metrics: %s", error)
            raise

    # cognee's TEMPORAL search type calls these two non-interface methods on
    # the graph engine (temporal_retriever.py) and would otherwise die with an
    # AttributeError; fail the way the CYPHER/NATURAL_LANGUAGE gates do. Real
    # implementations are planned (Phase 5 in the work plan).
    async def collect_time_ids(self, time_from=None, time_to=None):
        raise SearchTypeNotSupported(
            "Temporal search is not yet supported with the TypeDBAdapter graph backend."
        )

    async def collect_events(self, ids):
        raise SearchTypeNotSupported(
            "Temporal search is not yet supported with the TypeDBAdapter graph backend."
        )

    async def remove_belongs_to_set_tags(self, tags, node_ids=None) -> None:
        if not tags or (node_ids is not None and not node_ids):
            return None
        tag_set = set(tags)

        def mutate(_node_id, properties):
            current = properties.get("belongs_to_set")
            if not isinstance(current, list) or not any(tag in tag_set for tag in current):
                return None
            return {**properties, "belongs_to_set": [tag for tag in current if tag not in tag_set]}

        identities = None if node_ids is None else [str(node_id) for node_id in node_ids]
        await self._mutate_properties("node", identities, mutate)
        return None

    # --- feedback / truth weights (stored in properties-json, as Ladybug does;
    # CogneeGraph reads feedback_weight from the projected properties) ---

    @staticmethod
    def _non_empty_string_ids(ids) -> list[str]:
        return [identity for identity in ids if isinstance(identity, str) and identity]

    async def get_node_feedback_weights(self, node_ids) -> dict[str, float]:
        valid = self._non_empty_string_ids(node_ids)
        if not valid:
            return {}
        result = {}
        for node in await self.get_nodes(valid):
            try:
                result[node["id"]] = float(node.get("feedback_weight", 0.5))
            except (TypeError, ValueError):
                result[node["id"]] = 0.5
        return result

    async def set_node_feedback_weights(self, node_feedback_weights) -> dict[str, bool]:
        if not node_feedback_weights:
            return {}
        valid = self._non_empty_string_ids(node_feedback_weights)
        updated = set()
        if valid:
            updated = await self._mutate_properties(
                "node",
                valid,
                lambda node_id, props: {
                    **props,
                    "feedback_weight": float(node_feedback_weights[node_id]),
                },
            )
        return {node_id: node_id in updated for node_id in node_feedback_weights}

    async def get_node_truth_state(self, node_ids) -> dict[str, dict[str, Any]]:
        valid = self._non_empty_string_ids(node_ids)
        if not valid:
            return {}
        result = {}
        for node in await self.get_nodes(valid):
            alignment = node.get("truth_alignment", [])
            epoch = node.get("truth_epoch")
            try:
                truth_epoch = int(epoch) if epoch is not None else None
            except (TypeError, ValueError):
                truth_epoch = None
            result[node["id"]] = {
                "truth_alignment": list(alignment) if isinstance(alignment, (list, tuple)) else [],
                "truth_epoch": truth_epoch,
            }
        return result

    async def set_node_truth_state(self, node_truth_state) -> dict[str, bool]:
        if not node_truth_state:
            return {}
        valid = self._non_empty_string_ids(node_truth_state)

        def mutate(node_id, props):
            state = node_truth_state[node_id]
            updated = {**props, "truth_alignment": list(state.get("truth_alignment") or [])}
            if state.get("truth_epoch") is not None:
                updated["truth_epoch"] = int(state["truth_epoch"])
            return updated

        updated = await self._mutate_properties("node", valid, mutate) if valid else set()
        return {node_id: node_id in updated for node_id in node_truth_state}

    async def _edges_by_object_ids(self, edge_object_ids) -> list[dict]:
        rows = [{"v": edge_object_id} for edge_object_id in dict.fromkeys(edge_object_ids)]
        return (await self._read_batch([(_EDGES_BY_OBJECT_ID, rows)]))[0]

    async def get_edge_feedback_weights(self, edge_object_ids) -> dict[str, float]:
        valid = self._non_empty_string_ids(edge_object_ids)
        if not valid:
            return {}
        result = {}
        for document in await self._edges_by_object_ids(valid):
            properties = self._document_to_edge_properties({"properties-json": document.get("p")})
            try:
                result[document["eoid"]] = float(properties.get("feedback_weight", 0.5))
            except (TypeError, ValueError):
                result[document["eoid"]] = 0.5
        return result

    async def set_edge_feedback_weights(self, edge_feedback_weights) -> dict[str, bool]:
        if not edge_feedback_weights:
            return {}
        valid = self._non_empty_string_ids(edge_feedback_weights)
        found = (
            {doc["key"]: doc["eoid"] for doc in await self._edges_by_object_ids(valid)}
            if valid
            else {}
        )
        updated_keys = set()
        if found:
            updated_keys = await self._mutate_properties(
                "edge",
                list(found),
                lambda key, props: {
                    **props,
                    "feedback_weight": float(edge_feedback_weights[found[key]]),
                },
            )
        updated = {found[key] for key in updated_keys}
        return {
            edge_object_id: edge_object_id in updated for edge_object_id in edge_feedback_weights
        }

    async def get_triplets_batch(self, offset: int, limit: int) -> list[dict[str, Any]]:
        """Edges as {start_node, relationship_properties, end_node}, ordered by edge-key."""
        if offset < 0:
            raise ValueError(f"Offset must be non-negative, got {offset}")
        if limit < 0:
            raise ValueError(f"Limit must be non-negative, got {limit}")
        if limit == 0:
            return []
        query = _TRIPLETS_BATCH.format(offset=int(offset), limit=int(limit))
        triplets = []
        for document in (await self._read_batch([query]))[0]:
            edge_doc = document["edge"]
            triplets.append(
                {
                    "start_node": self._document_to_node_dict(document["start"]),
                    "relationship_properties": {
                        **self._document_to_edge_properties(edge_doc),
                        "relationship_name": edge_doc.get("relationship-name"),
                    },
                    "end_node": self._document_to_node_dict(document["end"]),
                }
            )
        return triplets

    async def is_empty(self) -> bool:
        results = await self._read_batch(["match $n isa node; limit 1; reduce $count = count;"])
        if not results[0]:
            return True  # no database (or no rows): nothing to search
        return results[0][0].get("count", 1) == 0
