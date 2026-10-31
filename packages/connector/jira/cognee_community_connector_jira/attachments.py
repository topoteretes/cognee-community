"""Text decoding and isolated, deadline-bounded PDF extraction."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json", "application/xml"}


def extract_text(data: bytes, mime: str, max_chars: int = 200_000, timeout: float = 10) -> str:
    if mime in TEXT_TYPES:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("Supported text attachment is not UTF-8; snapshot aborted") from None
    elif mime == "application/pdf":
        # Run the worker file directly: do not import Cognee/dlt in the child.
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), str(max_chars)],
                input=data,
                capture_output=True,
                timeout=timeout,
                check=False,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired:
            raise ValueError("PDF extraction exceeded its deadline; snapshot aborted") from None
        if result.returncode != 0 or len(result.stdout) > max_chars * 12 + 1024:
            raise ValueError("PDF extraction failed or exceeded its budget; snapshot aborted")
        try:
            text = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise ValueError("PDF extraction returned an invalid result") from None
    else:
        raise ValueError("Unsupported attachment extraction requested")
    if not isinstance(text, str) or len(text) > max_chars:
        raise ValueError("Attachment text exceeded its character budget")
    return text.strip() or "[No extractable text; OCR is not supported]"


def _pdf_worker():
    from pypdf import PdfReader

    maximum = int(sys.argv[1])
    reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()), strict=True)
    if reader.is_encrypted or len(reader.pages) > 100:
        raise ValueError("PDF unsupported")
    parts, size = [], 0
    for page in reader.pages:
        text = page.extract_text() or ""
        size += len(text) + 1
        if size > maximum:
            raise ValueError("PDF too long")
        parts.append(text)
    print(json.dumps("\n".join(parts)))


if __name__ == "__main__":
    try:
        _pdf_worker()
    except Exception:
        # Never return exception text, filenames, PDF metadata or stderr content.
        raise SystemExit(1) from None
