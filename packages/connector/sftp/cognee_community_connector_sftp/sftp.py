"""SFTP connector for cognee: a ``dlt`` source that turns files on an SFTP server into memory.

Sync the text files and PDFs under one or more folders of an SFTP server into
cognee, incrementally and with forget-on-delete. The resource is handed directly
to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_sftp import sftp_source

    await cognee.remember(
        sftp_source(
            host="files.example.com",
            username="cognee",
            private_key_path="~/.ssh/cognee_ed25519",
            roots=["/reports"],
        ),
        dataset_name="sftp",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per file
  (``file:<path>``), titled with the file's path and holding its text.
* **The host key is always verified**, against ``known_hosts`` or a pinned SHA256
  fingerprint, and never accepted blindly. Only the password or key passed in is
  tried: no SSH agent and no keys from ``~/.ssh``.
* **Incremental.** Each folder is listed once per run, which gives every file's
  size and mtime, and only new or changed files are downloaded. A file whose bytes
  did not change (touched, or rewritten with the same content) is not emitted
  again. mtimes are whole seconds, so a changed file that is the newest in its
  folder is downloaded once more on the next run, which catches a rewrite of the
  same size within the second it was listed.
* **Forget-on-delete.** A file missing from its folder's listing (deleted,
  renamed, moved away), filtered out, or under a root that is no longer selected
  is emitted as a hard-delete tombstone, and so is every file under a folder that
  disappeared. A root that is missing or cannot be read raises, a subfolder that
  cannot be read is left as it is, and a root that is suddenly empty (as an
  unmounted share looks) is forgotten only once the next run confirms it, so a
  typo, a permission change or a mount problem never forgets anything.
* **A run is bounded** by a request budget. A run cut short by it, or by a lost
  connection, keeps the files it finished and the next run continues where it
  stopped.

Privacy
-------
This reads every supported file the account can read under the selected roots.
Nothing is fetched until you construct a source and call ``remember``, and anyone
with read access to the target dataset can read what was ingested.
"""

import base64
import fnmatch
import hashlib
import io
import json
import os
import posixpath
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("sftp_connector")

DEFAULT_PORT = 22
# Connect, banner, auth and per-request timeout. A healthy server answers in well
# under a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Folder listings and file downloads of one run. At tens of milliseconds a round
# trip that is a minute or two, so a run over a huge tree hands back cognee's
# ingestion lock in good time and the next run continues.
DEFAULT_MAX_REQUESTS = 1000
# Thousands of pages of text or PDF. Bigger files are usually exports or archives,
# which read poorly as one document.
DEFAULT_MAX_FILE_SIZE_MB = 25
TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".rst"}
)
PDF_EXTENSIONS = frozenset({".pdf"})
# OpenSSH's format, as `ssh-keygen -lf` prints it: base64 of a SHA-256 digest
# without the padding.
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_RESOURCE_NAME = re.compile(r"[a-z0-9_]{1,64}")
_STATS = (
    "scanned",
    "emitted",
    "unchanged",
    "deleted",
    "gone",
    "unreadable",
    "skipped",
    "too_large",
    "unparseable",
    "empty",
    "failed",
    "failed_budget",
    "failed_connection",
)


# ---------------------------------------------------------------------------
# Errors: messages never carry the password, the passphrase or file contents.
# ---------------------------------------------------------------------------
class SFTPSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class SFTPAuthError(SFTPSourceError):
    """The server rejected the credentials, or the private key could not be loaded."""


class SFTPHostKeyError(SFTPSourceError):
    """The server's host key is not known, or does not match the expected one."""


class SFTPConnectionError(SFTPSourceError):
    """The server could not be reached, or the connection was lost."""


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def host_key_fingerprint(key: Any) -> str:
    """The ``SHA256:...`` fingerprint of a paramiko key, as ``ssh-keygen -lf`` prints it."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _host_key_policy(pinned: str | None) -> Any:
    """Accept a host missing from known_hosts only when its key has the pinned fingerprint."""
    import paramiko

    class _PinnedHostKey(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            presented = host_key_fingerprint(key)
            if pinned is None:
                raise SFTPHostKeyError(
                    f"{hostname} is not in known_hosts. Its host key is {presented}: check "
                    "it with the server's administrator, then add the host to known_hosts "
                    "or pass host_key_fingerprint="
                )
            if presented != pinned:
                raise SFTPHostKeyError(
                    f"The host key of {hostname} is {presented}, not the pinned {pinned}"
                )

    return _PinnedHostKey()


class SFTPClient:
    """Minimal SFTP client over paramiko, which connects on first use.

    ``stat``, ``listdir_attr`` and ``read`` raise :class:`FileNotFoundError`,
    :class:`PermissionError` or another :class:`OSError` as the server reports
    them, and :class:`SFTPConnectionError` when the connection is lost. Listings do
    not follow symlinks. Tests and hosts can pass any object with the same methods
    to :func:`sftp_source` instead.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        username: str = "",
        *,
        password: str | None = None,
        private_key_path: str | None = None,
        private_key_passphrase: str | None = None,
        known_hosts: str | None = None,
        host_key_fingerprint: str | None = None,
        timeout: float = _TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self._key_path = private_key_path
        self._passphrase = private_key_passphrase
        self._known_hosts = known_hosts
        self._pinned = host_key_fingerprint
        self._timeout = timeout
        self._ssh: Any = None
        self._sftp: Any = None

    def __repr__(self) -> str:
        return (
            f"SFTPClient(host={self.host!r}, port={self.port}, "
            f"username={self.username!r}, password=<redacted>)"
        )

    def _load_key(self) -> Any:
        if not self._key_path:
            return None
        import paramiko

        # paramiko takes the passphrase as bytes, and renamed its keyword in 5.0.
        passphrase = self._passphrase.encode() if self._passphrase else None
        try:
            return paramiko.PKey.from_path(self._key_path, passphrase)
        except FileNotFoundError:
            raise SFTPAuthError(f"Private key file {self._key_path} not found") from None
        except Exception as exc:
            # Missing or wrong passphrase, or not a private key paramiko reads.
            raise SFTPAuthError(
                f"Private key {self._key_path} could not be loaded ({type(exc).__name__}): "
                "check the passphrase and the key type"
            ) from None

    def _connect(self) -> None:
        import paramiko

        key = self._load_key()
        ssh = paramiko.SSHClient()
        try:
            if self._known_hosts:
                # The system set is never written back, unlike load_host_keys().
                ssh.load_system_host_keys(self._known_hosts)
            ssh.set_missing_host_key_policy(_host_key_policy(self._pinned))
            ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self._password,
                pkey=key,
                timeout=self._timeout,
                banner_timeout=self._timeout,
                auth_timeout=self._timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            sftp = ssh.open_sftp()
            sftp.get_channel().settimeout(self._timeout)
        except SFTPSourceError:
            ssh.close()
            raise
        except paramiko.BadHostKeyException as exc:
            ssh.close()
            raise SFTPHostKeyError(
                f"The host key of {exc.hostname} is {host_key_fingerprint(exc.key)}, not "
                f"{host_key_fingerprint(exc.expected_key)} as in known_hosts"
            ) from None
        except paramiko.AuthenticationException:
            ssh.close()
            raise SFTPAuthError(
                f"{self.host} rejected the credentials of {self.username!r}"
            ) from None
        except (paramiko.SSHException, OSError, EOFError) as exc:
            ssh.close()
            raise SFTPConnectionError(
                f"Could not connect to {self.host}:{self.port} ({type(exc).__name__}: {exc})"
            ) from exc
        self._ssh, self._sftp = ssh, sftp

    def _run(self, operation: Callable[[Any], Any]) -> Any:
        import paramiko

        if self._sftp is None:
            self._connect()
        try:
            return operation(self._sftp)
        except (FileNotFoundError, PermissionError):
            raise
        except (paramiko.SSHException, EOFError, TimeoutError) as exc:
            raise SFTPConnectionError(
                f"The connection to {self.host} was lost ({type(exc).__name__})"
            ) from exc
        except OSError as exc:
            transport = self._ssh.get_transport()
            if transport is None or not transport.is_active():
                raise SFTPConnectionError(
                    f"The connection to {self.host} was lost ({type(exc).__name__})"
                ) from exc
            # The server failed this one request (an SFTP "Failure").
            raise

    def stat(self, path: str) -> Any:
        return self._run(lambda sftp: sftp.stat(path))

    def listdir_attr(self, path: str) -> list[Any]:
        return self._run(lambda sftp: sftp.listdir_attr(path))

    def read(self, path: str, limit: int) -> bytes:
        """The file's bytes, at most ``limit + 1`` of them, so a larger file is noticed."""

        def read(sftp: Any) -> bytes:
            with sftp.open(path, "rb") as handle:
                # Pipelined reads instead of one round trip per 32 KB block.
                handle.prefetch(min(handle.stat().st_size or 0, limit + 1))
                return handle.read(limit + 1)

        return self._run(read)

    def close(self) -> None:
        if self._ssh is not None:
            self._ssh.close()
        self._ssh = self._sftp = None


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------
def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("an encrypted PDF")
        pages = [page.extract_text() or "" for page in reader.pages]
    except ValueError:
        raise
    except Exception as exc:
        # pypdf raises many types on damaged files.
        raise ValueError(f"an unreadable PDF ({type(exc).__name__})") from None
    return "\n\n".join(page.strip() for page in pages if page.strip())


def extract_text(name: str, data: bytes) -> str:
    """The text of a file, by its extension. Raises :class:`ValueError` when it has none."""
    if posixpath.splitext(name)[1].lower() in PDF_EXTENSIONS:
        return _pdf_text(data)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("not UTF-8 text") from None
    if "\x00" in text:
        raise ValueError("binary content")
    return text


def _row_id(path: str) -> str:
    return f"file:{path}"


def _url(host: str, port: int, path: str) -> str:
    netloc = f"[{host}]" if ":" in host else host
    if port != DEFAULT_PORT:
        netloc += f":{port}"
    return f"sftp://{netloc}{quote(path, safe='/')}"


def render_file(host: str, port: int, path: str, text: str) -> dict[str, Any]:
    return {
        "id": _row_id(path),
        # cognee puts the title on top of the document as a heading.
        "title": path,
        "content": text.strip(),
        "url": _url(host, port, path),
        "_deleted": False,
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _SFTPConfig:
    host: str
    port: int
    roots: tuple[str, ...]
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_file_size: int = DEFAULT_MAX_FILE_SIZE_MB * 1024 * 1024
    max_requests: int = DEFAULT_MAX_REQUESTS

    def selection(self) -> str:
        """What decides which files are synced. A change restarts the walk."""
        return _digest(
            [self.roots, self.include, self.exclude, self.max_file_size, self.host, self.port]
        )

    def root_of(self, path: str) -> str | None:
        for root in self.roots:
            if path == root or path.startswith(root.rstrip("/") + "/"):
                return root
        return None


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the files finished so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _matches(relative: str, patterns: tuple[str, ...]) -> bool:
    """A pattern matches the path under the root; one without ``/`` also the bare name."""
    name = posixpath.basename(relative)
    return any(
        fnmatch.fnmatchcase(relative, pattern)
        or ("/" not in pattern and fnmatch.fnmatchcase(name, pattern))
        for pattern in patterns
    )


class _Walker:
    """One run over the selected roots, writing progress into ``state``.

    ``state["folders"]`` maps each folder to its synced files, ``state["queue"]``
    holds the folders still to list in the walk in progress, so a run cut short
    continues where it stopped.
    """

    def __init__(
        self,
        client: Any,
        config: _SFTPConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.folders: dict[str, dict[str, dict]] = state.setdefault("folders", {})
        self.subfolders: dict[str, set[str]] = {}
        self.requests = 0
        # Set once a folder or a download is finished; the budget never stops a
        # run before that.
        self.progressed = False

    def _call(self, method: str, *args: Any) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return getattr(self.client, method)(*args)
        except SFTPConnectionError:
            # Lost during the run: keep what is done. On the first request it is a
            # server that cannot be reached, which raises.
            if self.requests > 1:
                raise _CutShortError("connection") from None
            raise

    # -- forgetting --------------------------------------------------------
    def _forget_file(self, folder: str, name: str) -> Iterator[dict]:
        files = self.folders.get(folder, {})
        entry = files.pop(name, None)
        if not files:
            self.folders.pop(folder, None)
        if entry and entry.get("doc"):
            self.stats["deleted"] += 1
            yield {"id": _row_id(posixpath.join(folder, name)), "_deleted": True}

    def _forget_tree(self, path: str) -> Iterator[dict]:
        prefix = path.rstrip("/") + "/"
        for folder in [f for f in self.folders if f == path or f.startswith(prefix)]:
            for name in list(self.folders.get(folder, {})):
                yield from self._forget_file(folder, name)
            self.state.get("empty", {}).pop(folder, None)

    def _index_subfolders(self) -> None:
        """Which subfolders of each folder hold synced files, at any depth."""
        for folder in self.folders:
            root = self.config.root_of(folder)
            path = folder
            while root is not None and path != root:
                parent = posixpath.dirname(path)
                self.subfolders.setdefault(parent, set()).add(posixpath.basename(path))
                path = parent

    # -- the run -----------------------------------------------------------
    def _check_roots(self) -> None:
        for root in self.config.roots:
            try:
                attrs = self._call("stat", root)
            except FileNotFoundError:
                raise SFTPSourceError(f"The SFTP root {root} does not exist") from None
            except PermissionError:
                raise SFTPSourceError(f"The SFTP root {root} cannot be read") from None
            if not stat.S_ISDIR(attrs.st_mode or 0):
                raise SFTPSourceError(f"The SFTP root {root} is not a folder")

    def rows(self) -> Iterator[dict]:
        self._check_roots()
        selection = self.config.selection()
        if self.state.get("selection") != selection:
            # A walk started under other settings starts over.
            self.state.pop("queue", None)
            self.state["selection"] = selection
        # A root that is no longer selected is forgotten as a whole.
        for folder in [f for f in self.folders if self.config.root_of(f) is None]:
            for name in list(self.folders.get(folder, {})):
                yield from self._forget_file(folder, name)
            self.state.get("empty", {}).pop(folder, None)
        self._index_subfolders()
        queue = self.state.setdefault("queue", sorted(self.config.roots))
        while queue:
            folder = queue[0]
            subfolders = yield from self._visit(folder)
            queue.pop(0)
            queue[:0] = subfolders
            self.progressed = True
        del self.state["queue"]

    def _visit(self, folder: str) -> Iterator[dict]:
        """Sync one folder; returns its subfolders to walk next."""
        root = self.config.root_of(folder) or folder
        try:
            entries = self._call("listdir_attr", folder)
        except FileNotFoundError:
            if folder == root:
                raise SFTPSourceError(f"The SFTP root {root} disappeared during the run") from None
            # Removed since its parent was listed; the parent's next listing forgets it.
            return []
        except OSError as exc:
            if folder == root:
                raise SFTPSourceError(f"The SFTP root {root} cannot be read") from exc
            logger.warning("SFTP: %s cannot be listed (%s), left unchanged.", folder, exc)
            self.stats["unreadable"] += 1
            return []
        files, dirs = self._classify(folder, root, entries)
        known = self.folders.get(folder, {})
        known_dirs = self.subfolders.get(folder, set())
        # An unmounted share looks like an empty folder.
        if folder == root and not entries and (known or known_dirs):
            if not self._confirm_empty(folder):
                return []
        else:
            self.state.get("empty", {}).pop(folder, None)
        # Decided from the listing, so a run cut short later still forgets them.
        for name in [n for n in known if n not in files]:
            yield from self._forget_file(folder, name)
        for name in sorted(known_dirs - set(dirs)):
            yield from self._forget_tree(posixpath.join(folder, name))
        newest = max((attrs.st_mtime for attrs in files.values()), default=None)
        for name in sorted(files):
            yield from self._sync_file(folder, name, files[name], newest)
        return [posixpath.join(folder, name) for name in sorted(dirs)]

    def _classify(
        self, folder: str, root: str, entries: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        files: dict[str, Any] = {}
        dirs: dict[str, Any] = {}
        for attrs in entries or ():
            name = getattr(attrs, "filename", None)
            mode = getattr(attrs, "st_mode", None)
            if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or mode is None:
                self.stats["skipped"] += 1
                continue
            relative = posixpath.relpath(posixpath.join(folder, name), root)
            if stat.S_ISDIR(mode):
                if not _matches(relative, self.config.exclude):
                    dirs[name] = attrs
            elif stat.S_ISREG(mode):
                self.stats["scanned"] += 1
                if self._wanted(relative, attrs):
                    files[name] = attrs
            else:
                # Symlinks are not followed, so nothing outside the roots is read and
                # no loop is walked. Sockets, devices and pipes have no content.
                self.stats["skipped"] += 1
        return files, dirs

    def _wanted(self, relative: str, attrs: Any) -> bool:
        if not isinstance(attrs.st_size, int) or not isinstance(attrs.st_mtime, int):
            self.stats["skipped"] += 1
            return False
        extension = posixpath.splitext(relative)[1].lower()
        if extension not in TEXT_EXTENSIONS | PDF_EXTENSIONS:
            self.stats["skipped"] += 1
            return False
        if self.config.include and not _matches(relative, self.config.include):
            return False
        if _matches(relative, self.config.exclude):
            return False
        if attrs.st_size > self.config.max_file_size:
            self.stats["too_large"] += 1
            return False
        return True

    def _confirm_empty(self, folder: str) -> bool:
        """An empty listing of a root that had files is confirmed by the next walk."""
        empty = self.state.setdefault("empty", {})
        if empty.pop(folder, False):
            return True
        empty[folder] = True
        logger.warning(
            "SFTP listed the root %s as empty although files were synced from it; nothing is "
            "forgotten until the next run confirms it.",
            folder,
        )
        return False

    def _sync_file(self, folder: str, name: str, attrs: Any, newest: int | None) -> Iterator[dict]:
        entry = self.folders.get(folder, {}).get(name)
        listed = [attrs.st_size, attrs.st_mtime]
        if entry and [entry["size"], entry["mtime"]] == listed and not entry.get("recheck"):
            self.stats["unchanged"] += 1
            return
        path = posixpath.join(folder, name)
        try:
            data = self._call("read", path, self.config.max_file_size)
        except FileNotFoundError:
            # Deleted between the listing and this read.
            self.stats["gone"] += 1
            yield from self._forget_file(folder, name)
            return
        except OSError as exc:
            logger.warning("SFTP: %s cannot be read (%s), left unchanged.", path, exc)
            self.stats["unreadable"] += 1
            return
        self.progressed = True
        new = {"size": attrs.st_size, "mtime": attrs.st_mtime, "sha256": "", "doc": False}
        # A same-size rewrite within the listed second keeps size and mtime, so a
        # changed file that is the newest in its folder is read once more next run.
        if (
            entry is None or [entry["size"], entry["mtime"]] != listed
        ) and attrs.st_mtime == newest:
            new["recheck"] = True
        if len(data) > self.config.max_file_size:
            self.stats["too_large"] += 1
            yield from self._forget_file(folder, name)
            return
        new["sha256"] = hashlib.sha256(data).hexdigest()
        if entry and entry.get("sha256") == new["sha256"]:
            new["doc"] = entry.get("doc", False)
            self.stats["unchanged"] += 1
            self.folders.setdefault(folder, {})[name] = new
            return
        text = self._text(path, data)
        if text is None:
            # Remembered by its hash, so it is not downloaded again until it changes.
            yield from self._forget_file(folder, name)
            self.folders.setdefault(folder, {})[name] = new
            return
        new["doc"] = True
        self.folders.setdefault(folder, {})[name] = new
        self.stats["emitted"] += 1
        yield render_file(self.config.host, self.config.port, path, text)

    def _text(self, path: str, data: bytes) -> str | None:
        """The file's text, or ``None`` (counted) when it has none to ingest."""
        try:
            text = extract_text(posixpath.basename(path), data)
        except ValueError as exc:
            logger.warning("SFTP: %s is %s, skipped.", path, exc)
            self.stats["unparseable"] += 1
            return None
        if not text.strip():
            self.stats["empty"] += 1
            return None
        return text


def _iter_rows(
    client: Any,
    config: _SFTPConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict.

    Rejected credentials, an unknown host key and a missing root raise, and are
    never read as "gone".
    """
    for key in _STATS:
        stats.setdefault(key, 0)
    try:
        yield from _Walker(client, config, state, stats, check_active).rows()
    except _CutShortError as cut:
        # A spent budget is a planned pause, not a failure; failed_budget says why.
        if cut.reason != "budget":
            stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("SFTP sync stopped early (%s); it continues on the next run.", cut.reason)
        return
    logger.info(
        "SFTP: %d file(s) emitted, %d unchanged, %d deleted.",
        stats["emitted"],
        stats["unchanged"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _clean_host(host: str | None) -> str:
    host = (host or "").strip()
    if not host:
        raise ValueError("An SFTP host is required")
    if "://" in host or "/" in host or any(c.isspace() for c in host):
        raise ValueError(f"The SFTP host {host!r} must be a host name or address, not a URL")
    return host.strip("[]")


def _clean_roots(roots: list[str] | None) -> tuple[str, ...]:
    cleaned: list[str] = []
    for root in roots or ():
        root = str(root).strip()
        if not root.startswith("/"):
            raise ValueError(f"The SFTP root {root!r} must be an absolute path such as /reports")
        root = posixpath.normpath(root)
        root = "/" + root.lstrip("/")
        if root not in cleaned:
            cleaned.append(root)
    if not cleaned:
        raise ValueError("Select at least one folder to sync with roots=")
    for root in cleaned:
        for other in cleaned:
            if other != root and (root == "/" or other.startswith(root + "/")):
                raise ValueError(f"The SFTP roots {root} and {other} overlap")
    return tuple(cleaned)


def _patterns(values: list[str] | None, what: str) -> tuple[str, ...]:
    patterns = tuple(str(value).strip() for value in values or ())
    if any(not pattern for pattern in patterns):
        raise ValueError(f"SFTP {what} patterns must not be empty")
    return patterns


def _default_resource_name(host: str, port: int, username: str, roots: tuple[str, ...]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{host}_{'_'.join(sorted(roots))}".lower()).strip("_")
    # Host and path case and punctuation are lost in the slug; the hash keeps
    # two servers, accounts or roots apart.
    scope = _digest([host.lower(), port, username, sorted(roots)])[:8]
    return f"sftp_{slug[:46].strip('_')}_{scope}"


def _check_credentials(
    username: str, password: str | None, private_key_path: str | None
) -> str | None:
    """Validate the login settings; returns the expanded key path."""
    if not username:
        raise ValueError("An SFTP username is required")
    if not password and not private_key_path:
        raise ValueError("Pass password=, private_key_path=, or both")
    if not private_key_path:
        return None
    private_key_path = os.path.expanduser(private_key_path)
    if not os.path.isfile(private_key_path):
        raise ValueError(f"The private key file {private_key_path} does not exist")
    return private_key_path


def _resolve_host_keys(known_hosts: str | None, fingerprint: str | None) -> str | None:
    if fingerprint is not None and not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError(
            "host_key_fingerprint must look like SHA256:<43 characters>, "
            "as `ssh-keygen -lf` prints it"
        )
    if known_hosts is not None:
        known_hosts = os.path.expanduser(known_hosts)
        if not os.path.isfile(known_hosts):
            raise ValueError(f"The known_hosts file {known_hosts} does not exist")
        return known_hosts
    if fingerprint is None:
        default = os.path.expanduser("~/.ssh/known_hosts")
        return default if os.path.isfile(default) else None
    return None


def sftp_source(
    *,
    host: str | None = None,
    username: str | None = None,
    roots: list[str] | None = None,
    port: int = DEFAULT_PORT,
    password: str | None = None,
    private_key_path: str | None = None,
    private_key_passphrase: str | None = None,
    known_hosts: str | None = None,
    host_key_fingerprint: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_file_size_mb: float = DEFAULT_MAX_FILE_SIZE_MB,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per file on an SFTP server.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        host: Host name or address of the SFTP server.
        username: Account to log in with.
        roots: Absolute folders to sync, each walked recursively. A root that is
            missing or cannot be read raises. Roots must not overlap.
        port: SSH port, default 22.
        password: Password of the account. Pass it, ``private_key_path``, or both.
        private_key_path: Path to an OpenSSH or PEM private key (Ed25519, ECDSA or RSA).
        private_key_passphrase: Passphrase of an encrypted private key.
        known_hosts: OpenSSH known_hosts file to verify the server against. Defaults
            to ``~/.ssh/known_hosts`` unless ``host_key_fingerprint`` is given.
        host_key_fingerprint: Pinned ``SHA256:...`` fingerprint of the server's host
            key (``ssh-keygen -lf``), accepted when the host is not in known_hosts.
        include: Glob patterns; only files matching one are synced. A pattern matches
            the path under its root (``*`` also matches ``/``), and one without ``/``
            also matches the file name.
        exclude: Glob patterns, matched the same way, for files and folders to leave
            out. An excluded folder is not walked.
        max_file_size_mb: Files bigger than this are skipped and counted in ``too_large``.
        max_requests: Request budget of one run (listings and downloads), checked once
            a folder or a download is finished. A bigger tree finishes over several
            runs; a spent budget sets ``failed_budget`` but not ``failed``.
        resource_name: Staging table name, which scopes this sync's state and
            cleanup: lowercase letters, digits and underscores, up to 64. Defaults to
            one derived from the host, port, account and roots.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row.
        service: Pre-built client (see :class:`SFTPClient`). Mainly an injection point
            for tests and hosts. ``client`` is accepted as an alias.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    host = _clean_host(host)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The SFTP port must be a whole number from 1 to 65535")
    if max_file_size_mb <= 0:
        raise ValueError("max_file_size_mb must be positive")
    if int(max_requests) < 1:
        raise ValueError("max_requests must be at least 1")
    config = _SFTPConfig(
        host=host,
        port=port,
        roots=_clean_roots(roots),
        include=_patterns(include, "include"),
        exclude=_patterns(exclude, "exclude"),
        max_file_size=int(max_file_size_mb * 1024 * 1024),
        max_requests=int(max_requests),
    )
    if resource_name is not None and not _RESOURCE_NAME.fullmatch(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, up to 64 characters"
        )
    username = (username or "").strip()
    service = service or client
    if service is None:
        # Checked now, so a bad setting fails here rather than inside the pipeline.
        private_key_path = _check_credentials(username, password, private_key_path)
        known_hosts = _resolve_host_keys(known_hosts, host_key_fingerprint)
    name = resource_name or _default_resource_name(host, port, username, config.roots)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def sftp_documents():
        stats.clear()
        state = dlt.current.resource_state()
        run_client = service or SFTPClient(
            host,
            port,
            username,
            password=password or None,
            private_key_path=private_key_path or None,
            private_key_passphrase=private_key_passphrase or None,
            known_hosts=known_hosts,
            host_key_fingerprint=host_key_fingerprint,
        )
        try:
            rows = _iter_rows(run_client, config, state, stats, check_active)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if service is None:
                run_client.close()

    resource = sftp_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "sftp")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
