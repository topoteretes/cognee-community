"""Shared NeuG connection management for the embedded NeuG database.

NeuG locks a database file exclusively per open mode: while one connection holds
it ``rw``, EVERY other opener is rejected with "Database locked" -- ``rw`` or
``r``, same-process or cross-process (verified in
``.neug_work/probe_rw_exclusivity.py``). Multiple ``r`` readers can share the file
only when NO writer holds it. Cognee's graph adapter and vector adapter both WRITE
to the SAME NeuG database (node/edge MERGEs, vector COPY, HNSW/FTS index builds,
deletes), so the process must hold ``rw`` for its lifetime -- which also rules out
serving reads from a separate read-only connection. All statements are therefore
serialized through one process-level read-write connection managed here:

- one ``Database(mode='rw')`` + one connection per process,
- an asyncio lock serializes every statement (reads and writes alike), so the
  concurrent ``graph.add_nodes`` / ``vector.create_data_points`` gather in
  ``add_data_points`` is safe by construction,
- a blocking single-thread executor keeps the native (synchronous) NeuG calls
  off the event loop while preserving statement ordering,
- reference counting: each adapter ``acquire()``s on construction and
  ``release()``s on ``close()``; the underlying database is only closed when
  the reference count reaches zero (or at interpreter exit), so one adapter
  being evicted from cognee's engine cache never tears the connection out
  from under the other.
"""

import asyncio
import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from cognee.shared.logging_utils import get_logger

logger = get_logger("NeuGConnectionManager")


def resolve_neug_db_path() -> str:
    """Resolve the NeuG database directory shared by graph and vector adapters.

    The ``NEUG_DB_PATH`` environment variable wins when set; otherwise the
    database lives under cognee's system root, next to the other local
    databases (``<system_root_directory>/databases/neug_db``).
    """
    env_path = os.environ.get("NEUG_DB_PATH")
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))

    from cognee.base_config import get_base_config

    base_config = get_base_config()
    return os.path.join(base_config.system_root_directory, "databases", "neug_db")


class NeuGConnectionManager:
    """Process-level owner of the single read-write NeuG connection."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db = None
        self._conn = None
        self._refcount = 0
        self._lifecycle_lock = threading.Lock()
        # Serializes every statement; a single rw connection cannot run two
        # statements concurrently anyway, and this keeps the gather() write
        # paths from interleaving mid-batch. The manager is a process-level
        # singleton but asyncio locks bind to the loop that first acquires
        # them, so the lock is recreated whenever the running loop changes
        # (scripts that asyncio.run() repeatedly, one loop per pytest test).
        self._statement_lock = asyncio.Lock()
        self._statement_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        # Single worker so the native blocking calls also stay ordered at the
        # thread level, not only at the asyncio level.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="neug-exec")
        self._closed_by_atexit = False

    # ------------------------------------------------------------------
    # Lifecycle / reference counting
    # ------------------------------------------------------------------

    def acquire(self) -> None:
        """Register a new adapter user and open the database if needed."""
        with self._lifecycle_lock:
            self._open_if_needed()
            self._refcount += 1
            logger.debug("NeuG connection acquired (refcount=%d)", self._refcount)

    def release(self) -> None:
        """Unregister an adapter user; close the database at refcount zero.

        A statement already running on the executor thread is safe here by
        construction: ``_execute_sync`` re-reads ``self._conn`` at run time,
        so a close racing a queued statement turns that statement into a
        clean RuntimeError instead of a use-after-close at the native layer.
        """
        with self._lifecycle_lock:
            self._refcount = max(0, self._refcount - 1)
            logger.debug("NeuG connection released (refcount=%d)", self._refcount)
            if self._refcount == 0:
                self._close_locked()

    def shutdown(self) -> None:
        """Unconditional close, registered via ``atexit`` (ghost-PK discipline)."""
        with self._lifecycle_lock:
            self._closed_by_atexit = True
            self._refcount = 0
            self._close_locked()

    def _open_if_needed(self) -> None:
        if self._conn is not None:
            return
        try:
            from neug import Database
        except ImportError as e:
            raise ImportError(
                "The 'neug' package is required for the NeuG backend but is not "
                "installed. Install it manually with `pip install 'neug>=0.2.0,<0.3'` "
                "and retry. (NeuG hard-pins protobuf==5.29.6, which conflicts with "
                "cognee's resolved protobuf, so it is intentionally not offered as a "
                "cognee extra.)"
            ) from e

        os.makedirs(self.db_path, exist_ok=True)
        self._db = Database(self.db_path, mode="rw")
        self._conn = self._db.connect()
        # The executor is torn down when the refcount hits zero (see
        # ``_close_locked``); recreate it if the manager is re-acquired.
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="neug-exec")
        # Extensions must be loaded per connection; vector_search provides
        # HNSW + vector_distance_cosine, fts provides FTS indexes + bm25.
        # A released wheel does not bundle the extension binaries, so on a
        # fresh install LOAD fails until INSTALL fetches them (a one-time,
        # checksum-verified network download from the NeuG extension repo).
        self._ensure_extension("vector_search")
        self._ensure_extension("fts")
        logger.info("Opened NeuG database at %s (read-write)", self.db_path)

    def _ensure_extension(self, name: str) -> None:
        """LOAD a NeuG extension, INSTALLing it first if the binary is absent.

        Released wheels do not bundle the ``vector_search`` / ``fts`` extension
        binaries, so the first connection on a fresh install fails ``LOAD``
        until ``INSTALL`` fetches the prebuilt, checksum-verified binary from
        the NeuG extension repository (a one-time network download; later
        connections LOAD it straight from disk). A failure to INSTALL (e.g.
        offline) is re-raised so the backend errors loudly instead of silently
        degrading to a connection without vector/FTS support.
        """
        try:
            self._conn.execute(f"LOAD {name}")
            return
        except Exception as load_err:  # noqa: BLE001 - extension not installed yet
            logger.info(
                "NeuG extension '%s' not loadable (%s); attempting INSTALL "
                "(one-time network download)...",
                name,
                load_err,
            )
        try:
            self._conn.execute(f"INSTALL {name}")
        except Exception as install_err:  # noqa: BLE001
            raise RuntimeError(
                f"Failed to INSTALL the NeuG extension '{name}', which the NeuG "
                "backend requires. It is downloaded on first use -- check network "
                f"access to the NeuG extension repository. Original error: {install_err}"
            ) from install_err
        self._conn.execute(f"LOAD {name}")
        logger.info("NeuG extension '%s' installed and loaded.", name)

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as e:
                logger.warning("Error closing NeuG connection: %s", e)
            self._conn = None
        if self._db is not None:
            try:
                self._db.close()
            except Exception as e:
                logger.warning("Error closing NeuG database: %s", e)
            self._db = None
        # Release the worker thread so a refcount-zero manager does not leave a
        # resident executor behind; ``_open_if_needed`` rebuilds it on re-acquire.
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _get_statement_lock(self) -> asyncio.Lock:
        """Return the statement lock bound to the currently running loop.

        A process-level singleton outlives any single event loop; reusing a
        lock that was first acquired on a different loop raises
        ``RuntimeError: ... is bound to a different event loop``, so the
        lock is recreated on loop change.
        """
        loop = asyncio.get_running_loop()
        if self._statement_lock_loop is not loop:
            self._statement_lock = asyncio.Lock()
            self._statement_lock_loop = loop
        return self._statement_lock

    def _execute_sync(
        self, query: str, params: Optional[Dict[str, Any]], access_mode: str
    ) -> List[List[Any]]:
        rows, _ = self._execute_sync_with_columns(query, params, access_mode)
        return rows

    def _execute_sync_with_columns(
        self, query: str, params: Optional[Dict[str, Any]], access_mode: str
    ) -> tuple[List[List[Any]], List[str]]:
        conn = self._conn
        if conn is None:
            raise RuntimeError("NeuG connection is closed.")
        if params:
            result = conn.execute(query, access_mode=access_mode, parameters=params)
        else:
            result = conn.execute(query, access_mode=access_mode)
        # QueryResult is one-shot iterable; consume it fully on the worker
        # thread so callers get plain Python data. Column names come back
        # prefixed per binding block (e.g. ``_0_n.name``) and are only needed
        # by the graph adapter's Cypher pass-through row shaping.
        rows = list(result)
        try:
            columns = list(result.column_names())
        except Exception:
            columns = []
        return rows, columns

    async def execute(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        access_mode: str = "",
    ) -> List[List[Any]]:
        """Execute one statement, serialized with every other statement."""
        async with self._get_statement_lock():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor, self._execute_sync, query, params, access_mode
            )

    async def execute_with_columns(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        access_mode: str = "",
    ) -> tuple[List[List[Any]], List[str]]:
        """Like ``execute`` but also returns the result column names."""
        async with self._get_statement_lock():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor, self._execute_sync_with_columns, query, params, access_mode
            )


_manager: Optional[NeuGConnectionManager] = None
_manager_lock = threading.Lock()


def get_neug_connection_manager(db_path: Optional[str] = None) -> NeuGConnectionManager:
    """Return the process-level shared NeuG connection manager (singleton)."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = NeuGConnectionManager(db_path or resolve_neug_db_path())
            atexit.register(_manager.shutdown)
        return _manager
