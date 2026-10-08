"""In-memory Dropbox for tests.

Speaks the parts of the official SDK the connector uses (files_list_folder,
files_list_folder_continue, files_download, files_export) and returns the SDK's
own metadata, result and error types, so the connector code under test runs
exactly as it would against the real API.  Every change made through the
helpers (put / move / delete) is appended to a change log, which is what
list_folder/continue cursors replay.
"""

import datetime
import posixpath

from dropbox import files
from dropbox.exceptions import ApiError

_NOW = datetime.datetime(2026, 1, 1)


def api_error(error) -> ApiError:
    return ApiError("request-id", error, None, None)


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.closed = False

    def close(self):
        self.closed = True


class FakeDropbox:
    def __init__(self, page_size: int = 2000):
        self.page_size = page_size
        self.files: dict[str, files.FileMetadata] = {}  # path_lower -> metadata
        self.folders: dict[str, files.FolderMetadata] = {}  # path_lower -> metadata
        self.content: dict[str, bytes] = {}  # file id -> bytes
        self.log: list = []  # ordered change log replayed by cursors
        # Test hooks.
        self.reset_cursors: set[str] = set()
        self.continue_errors: list[Exception] = []
        self.list_errors: list[Exception] = []
        self.download_errors: dict[str, Exception] = {}
        self.downloads: list[str] = []
        self.exports: list[tuple[str, str]] = []
        self._pages: dict[str, tuple[list, str]] = {}
        self._ids = 0
        self._revs = 0

    # ------------------------------------------------------------------
    # Helpers that change the fake Dropbox (and record the change)
    # ------------------------------------------------------------------
    def put(
        self,
        path: str,
        content: bytes | str = b"",
        *,
        is_downloadable: bool = True,
        export_info: files.ExportInfo | None = None,
        size: int | None = None,
    ) -> str:
        """Create or edit a file. Returns its id (kept on edits)."""
        data = content.encode() if isinstance(content, str) else content
        self._ensure_folder(posixpath.dirname(path))
        existing = self.files.get(path.lower())
        file_id = existing.id if existing else self._new_id()
        meta = self._file_meta(path, file_id, size if size is not None else len(data))
        if not is_downloadable:
            meta.is_downloadable = False
            meta.export_info = export_info
        self.files[path.lower()] = meta
        self.content[file_id] = data
        self.log.append(meta)
        return file_id

    def move(self, old_path: str, new_path: str) -> None:
        """Move or rename a file: the old path is deleted, the id reappears."""
        meta = self.files.pop(old_path.lower())
        self.log.append(self._deleted(meta.path_display))
        self._ensure_folder(posixpath.dirname(new_path))
        moved = self._file_meta(new_path, meta.id, meta.size)
        moved.is_downloadable = meta.is_downloadable
        moved.export_info = meta.export_info
        self.files[new_path.lower()] = moved
        self.log.append(moved)

    def delete(self, path: str) -> None:
        """Delete a file, or a folder and everything under it, as one entry."""
        lower = path.lower()
        for store in (self.files, self.folders):
            for key in [k for k in store if k == lower or k.startswith(lower + "/")]:
                del store[key]
        self.log.append(self._deleted(path))

    # ------------------------------------------------------------------
    # SDK surface used by the connector
    # ------------------------------------------------------------------
    def files_list_folder(self, path, recursive=False, limit=None, **_kwargs):
        if self.list_errors:
            raise self.list_errors.pop(0)
        assert recursive, "the connector always lists recursively"
        path = path.lower()
        if path and path not in self.folders:
            raise api_error(files.ListFolderError.path(files.LookupError.not_found))
        entries = [
            meta
            for store in (self.folders, self.files)
            for key, meta in sorted(store.items())
            if self._under(key, path) and key != path
        ]
        return self._page(entries, self._cursor(path))

    def files_list_folder_continue(self, cursor):
        if self.continue_errors:
            raise self.continue_errors.pop(0)
        if cursor in self._pages:
            entries, final_cursor = self._pages.pop(cursor)
            return self._page(entries, final_cursor)
        if cursor in self.reset_cursors:
            raise api_error(files.ListFolderContinueError.reset)
        path, position = cursor.rsplit("|", 1)
        if path and path not in self.folders:
            raise api_error(files.ListFolderContinueError.path(files.LookupError.not_found))
        entries = [
            entry
            for entry in self.log[int(position) :]
            if self._under(entry.path_lower, path) and entry.path_lower != path
        ]
        return self._page(entries, self._cursor(path))

    def files_download(self, path):
        meta = self._by_id(path)
        self.downloads.append(path)
        if path in self.download_errors:
            raise self.download_errors[path]
        return meta, FakeResponse(self.content[meta.id])

    def files_export(self, path, export_format=None):
        meta = self._by_id(path)
        self.exports.append((path, export_format))
        if path in self.download_errors:
            raise self.download_errors[path]
        return files.ExportResult(), FakeResponse(self.content[meta.id])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _page(self, entries: list, final_cursor: str) -> files.ListFolderResult:
        if len(entries) <= self.page_size:
            return files.ListFolderResult(entries=entries, cursor=final_cursor, has_more=False)
        token = f"page-{len(self._pages)}-{len(self.log)}-{len(entries)}"
        self._pages[token] = (entries[self.page_size :], final_cursor)
        return files.ListFolderResult(
            entries=entries[: self.page_size], cursor=token, has_more=True
        )

    def _cursor(self, path: str) -> str:
        return f"{path}|{len(self.log)}"

    def _by_id(self, path: str) -> files.FileMetadata:
        for meta in self.files.values():
            if meta.id == path:
                return meta
        raise api_error(files.DownloadError.path(files.LookupError.not_found))

    def _ensure_folder(self, path: str) -> None:
        if path in ("", "/") or path.lower() in self.folders:
            return
        self._ensure_folder(posixpath.dirname(path))
        meta = files.FolderMetadata(
            name=posixpath.basename(path),
            id=self._new_id(),
            path_lower=path.lower(),
            path_display=path,
        )
        self.folders[path.lower()] = meta
        self.log.append(meta)

    def _file_meta(self, path: str, file_id: str, size: int) -> files.FileMetadata:
        self._revs += 1
        return files.FileMetadata(
            name=posixpath.basename(path),
            id=file_id,
            client_modified=_NOW,
            server_modified=_NOW,
            rev=f"{self._revs:016x}",
            size=size,
            path_lower=path.lower(),
            path_display=path,
        )

    def _deleted(self, path: str) -> files.DeletedMetadata:
        return files.DeletedMetadata(
            name=posixpath.basename(path), path_lower=path.lower(), path_display=path
        )

    def _new_id(self) -> str:
        self._ids += 1
        return f"id:file{self._ids}"

    @staticmethod
    def _under(path: str, folder: str) -> bool:
        return folder == "" or path == folder or path.startswith(folder + "/")


def make_pdf(text: str) -> bytes:
    """A minimal one-page PDF whose text pypdf can extract."""
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
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
    return bytes(out)
