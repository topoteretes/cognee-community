"""In-memory stand-in for an SFTP server, shared by the test modules.

It answers like OpenSSH's sftp-server, as checked against ``atmoz/sftp``: listings
carry ``paramiko.SFTPAttributes`` with whole-second mtimes and do not follow
symlinks, a missing path (or a file listed as a folder) raises
``FileNotFoundError``, an unreadable one ``PermissionError``, and reading a folder
an ``OSError`` without an errno.
"""

import errno
import itertools
import posixpath
import stat

import paramiko
import pytest

from cognee_community_connector_sftp.sftp import SFTPConnectionError

HOST = "files.example.com"


def _not_found():
    return FileNotFoundError(errno.ENOENT, "No such file")


def _denied():
    return PermissionError(errno.EACCES, "Permission denied")


def make_pdf(text: str) -> bytes:
    """A one-page PDF whose page shows ``text`` in Helvetica."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


class FakeSFTP:
    """Implements ``stat``, ``listdir_attr``, ``read`` and ``close`` like an SFTP server."""

    def __init__(self):
        # path -> ("dir", None) | ("file", bytes) | ("link", target) | ("fifo", None)
        self.nodes: dict[str, tuple[str, object]] = {"/": ("dir", None)}
        self.mtimes: dict[str, int] = {"/": 0}
        self.denied: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.drop_after: int | None = None
        self.unreachable = False
        self.closed = False
        self.now = 1_793_000_000
        self._ticks = itertools.count(1)

    # -- setting the server up -------------------------------------------------
    def tick(self, seconds: int = 1) -> int:
        self.now += seconds
        return self.now

    def mkdir(self, path: str) -> None:
        parent = posixpath.dirname(path)
        if parent not in self.nodes:
            self.mkdir(parent)
        if path not in self.nodes:
            self.nodes[path] = ("dir", None)
            self.mtimes[path] = self.now

    def write(self, path: str, data: bytes | str, *, mtime: int | None = None) -> None:
        """Write a file the way an upload does: the mtime is the server's current second."""
        self.mkdir(posixpath.dirname(path))
        self.nodes[path] = ("file", data.encode() if isinstance(data, str) else data)
        self.mtimes[path] = self.now if mtime is None else mtime

    def remove(self, path: str) -> None:
        for key in [k for k in self.nodes if k == path or k.startswith(path + "/")]:
            del self.nodes[key]
            self.mtimes.pop(key, None)

    def rename(self, old: str, new: str) -> None:
        """Move a file or folder; like a real rename it keeps the mtimes."""
        self.mkdir(posixpath.dirname(new))
        for key in sorted(k for k in self.nodes if k == old or k.startswith(old + "/")):
            moved = new + key[len(old) :]
            self.nodes[moved] = self.nodes.pop(key)
            self.mtimes[moved] = self.mtimes.pop(key)

    def symlink(self, target: str, path: str) -> None:
        self.nodes[path] = ("link", target)
        self.mtimes[path] = self.now

    def special(self, path: str) -> None:
        self.nodes[path] = ("fifo", None)
        self.mtimes[path] = self.now

    # -- the server's answers ----------------------------------------------------
    def _record(self, method: str, path: str) -> None:
        self.calls.append((method, path))
        if self.unreachable:
            raise SFTPConnectionError("Could not connect to files.example.com:22 (timeout)")
        if self.drop_after is not None and len(self.calls) > self.drop_after:
            raise SFTPConnectionError("The connection to files.example.com was lost (EOFError)")

    def _check_access(self, path: str) -> None:
        for denied in self.denied:
            if path == denied or path.startswith(denied.rstrip("/") + "/"):
                raise _denied()

    def _attrs(self, path: str, *, follow: bool) -> paramiko.SFTPAttributes:
        kind, value = self.nodes[path]
        if follow and kind == "link":
            if value not in self.nodes:
                raise _not_found()
            return self._attrs(value, follow=True)
        attrs = paramiko.SFTPAttributes()
        attrs.filename = posixpath.basename(path)
        attrs.st_mtime = self.mtimes[path]
        attrs.st_atime = self.mtimes[path]
        attrs.st_uid, attrs.st_gid = 1001, 100
        if kind == "dir":
            attrs.st_mode, attrs.st_size = stat.S_IFDIR | 0o755, 4096
        elif kind == "file":
            attrs.st_mode, attrs.st_size = stat.S_IFREG | 0o644, len(value)
        elif kind == "link":
            attrs.st_mode, attrs.st_size = stat.S_IFLNK | 0o777, len(value)
        else:
            attrs.st_mode, attrs.st_size = stat.S_IFIFO | 0o644, 0
        return attrs

    def _resolve(self, path: str) -> str:
        """``path`` with every symlinked folder on the way replaced by its target."""
        resolved = "/"
        for part in [p for p in path.split("/") if p]:
            resolved = posixpath.join(resolved, part)
            node = self.nodes.get(resolved)
            if node is not None and node[0] == "link" and resolved != path:
                resolved = node[1]
        return resolved

    def stat(self, path):
        self._record("stat", path)
        path = self._resolve(path)
        self._check_access(posixpath.dirname(path))
        if path not in self.nodes:
            raise _not_found()
        return self._attrs(path, follow=True)

    def listdir_attr(self, path):
        self._record("listdir_attr", path)
        path = self._resolve(path)
        if path in self.nodes and self.nodes[path][0] == "link":
            path = self.nodes[path][1]
        if path not in self.nodes or self.nodes[path][0] != "dir":
            raise _not_found()
        self._check_access(path)
        prefix = path.rstrip("/") + "/"
        children = [
            k
            for k in self.nodes
            if k != path and k.startswith(prefix) and "/" not in k[len(prefix) :]
        ]
        return [self._attrs(child, follow=False) for child in sorted(children)]

    def read(self, path, limit):
        self._record("read", path)
        path = self._resolve(path)
        if path not in self.nodes:
            raise _not_found()
        self._check_access(path)
        kind, value = self.nodes[path]
        if kind == "link":
            return self.read(value, limit)
        if kind != "file":
            raise OSError("Failure")
        return value[: limit + 1]

    def close(self):
        self.closed = True

    # -- helpers for assertions ----------------------------------------------------
    def reads(self) -> list[str]:
        return [path for method, path in self.calls if method == "read"]

    def listings(self) -> list[str]:
        return [path for method, path in self.calls if method == "listdir_attr"]


@pytest.fixture
def sftp():
    return FakeSFTP()
