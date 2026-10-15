"""Browser history and bookmarks connector for cognee.

A ``dlt`` source that extracts research trails, documentation visits, and bookmarks
from local Chromium and Firefox browser profiles, with incremental sync and
forget-on-delete.

Design
------
* **Auth** — None. Reads the user's local browser profile files. Nothing leaves the
  machine except optional page content fetches (off by default).
* **Browsers** — Chromium family (Chrome, Edge, Brave, Vivaldi via SQLite ``History``
  and JSON ``Bookmarks``) and Firefox (via ``places.sqlite``). Profiles are auto-detected
  on Windows, macOS, and Linux, or an explicit directory/file path can be provided.
* **Document Mode** — Yields one text document per URL into cognee with metadata
  (title, URL, domain, visit count, first/last visit, bookmark folder). Stored text
  is indexed and cognified into the graph.
* **Locked Databases** — Browsers lock their databases while open. The connector copies
  the database and any accompanying WAL / SHM files to a temporary directory and
  operates strictly on the copy.
* **Two Epochs** — Chromium records time as WebKit microseconds since 1601-01-01 UTC;
  Firefox records microseconds since Unix epoch 1970-01-01 UTC. Both are converted to
  standard UTC datetimes and compared accurately against cursors.
* **Incremental Sync** — Uses a cursor on the browser's last visit timestamp. Only
  pages visited (or bookmarks added) since the cursor are re-read and upserted.
* **Forget-on-Delete** — Every sync executes a lightweight key scan of valid URL ids.
  Cleared history, deleted bookmarks, or URLs matching new domain exclusions emit
  ``_deleted=True`` hard-delete tombstones so cognee's orphan cleanup purges them
  from graph, vector, and relational memory. If copying or reading fails, the sync
  aborts cleanly rather than falsely assuming deletion.
* **Privacy & Noise Filtering** — Off-by-default page fetching, automatic redaction
  of sensitive query parameters (token, session, key, etc.), dropping tracking tags,
  and a built-in exclude list for internal schemes, localhost, banking, webmail,
  login/SSO endpoints, and search result pages.
* **Node Sets** — Each page is tagged with ``browser:<browser>`` and ``browser:<domain>``,
  plus ``browser:bookmarks/<folder>`` (and ``browser:bookmarks``) if bookmarked.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    from cognee.tasks.ingestion.dlt_utils import (
        DOCUMENT_SOURCE_ATTR,
        NODE_SET_COLUMN,
        PIPELINE_SCOPE_ATTR,
    )
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"
    PIPELINE_SCOPE_ATTR = "cognee_pipeline_scope"
    NODE_SET_COLUMN = "cognee_node_set"

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("browser_connector")
except ImportError:
    import logging

    logger = logging.getLogger("browser_connector")

BROWSER_SOURCE_NAME = "browser"
BROWSER_TABLE_NAME = "browser_pages"

# Difference between WebKit epoch (1601-01-01 00:00:00 UTC) and Unix epoch (1970-01-01 00:00:00 UTC) in microseconds
_CHROMIUM_EPOCH_DIFF_US = 11644473600000000

_INTERNAL_SCHEMES = frozenset(
    {
        "about",
        "blob",
        "brave",
        "chrome",
        "chrome-extension",
        "data",
        "edge",
        "file",
        "javascript",
        "moz-extension",
        "view-source",
        "vivaldi",
    }
)

_SENSITIVE_HOSTS = frozenset(
    {
        # Webmail
        "mail.google.com",
        "inbox.google.com",
        "outlook.live.com",
        "outlook.office.com",
        "mail.yahoo.com",
        "mail.proton.me",
        "mail.aol.com",
        # Banking & payments
        "chase.com",
        "bankofamerica.com",
        "wellsfargo.com",
        "citi.com",
        "citibank.com",
        "capitalone.com",
        "paypal.com",
        "venmo.com",
        "fidelity.com",
        "schwab.com",
        "vanguard.com",
        "robinhood.com",
        "coinbase.com",
        "usbank.com",
        "pnc.com",
        "barclays.co.uk",
        "hsbc.com",
        # Auth & SSO
        "accounts.google.com",
        "login.microsoftonline.com",
        "login.live.com",
        "appleid.apple.com",
        "auth0.com",
        "okta.com",
        "id.atlassian.com",
    }
)

_SENSITIVE_PATH_KEYWORDS = frozenset(
    {
        "2fa",
        "auth",
        "billing",
        "cart",
        "checkout",
        "forgot-password",
        "login",
        "logout",
        "mfa",
        "oauth",
        "password_reset",
        "payments",
        "reset-password",
        "sign-in",
        "sign-out",
        "signin",
        "signout",
        "sso",
        "verify",
    }
)

_SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "auth_code",
        "auth_token",
        "code",
        "credential",
        "id_token",
        "jwt",
        "key",
        "nonce",
        "pass",
        "password",
        "pwd",
        "refresh_token",
        "secret",
        "session",
        "session_id",
        "sessionid",
        "sig",
        "signature",
        "state",
        "token",
        "verification_code",
    }
)

_TRACKING_QUERY_PARAMS = frozenset(
    {
        "_ga",
        "_gl",
        "campaign",
        "dclid",
        "fbclid",
        "gclid",
        "gclsrc",
        "mc_eid",
        "msclkid",
        "ref",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)

_SEARCH_ENGINES = (
    ("google.", "/search"),
    ("bing.com", "/search"),
    ("duckduckgo.com", "/"),
    ("search.yahoo.com", "/search"),
    ("ecosia.org", "/search"),
    ("yandex.", "/search"),
    ("baidu.com", "/s"),
)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|header|footer|nav)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Epoch helpers
# ---------------------------------------------------------------------------


def chromium_us_to_datetime(us: int) -> datetime:
    """Convert WebKit timestamp (microseconds since 1601-01-01) to UTC datetime."""
    if us <= 0:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    from datetime import timedelta

    unix_seconds = (us - _CHROMIUM_EPOCH_DIFF_US) / 1_000_000.0
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=unix_seconds)


def datetime_to_chromium_us(dt: datetime) -> int:
    """Convert UTC datetime to WebKit timestamp (microseconds since 1601-01-01)."""
    return int(dt.timestamp() * 1_000_000 + _CHROMIUM_EPOCH_DIFF_US)


def firefox_us_to_datetime(us: int) -> datetime:
    """Convert Firefox timestamp (microseconds since 1970-01-01) to UTC datetime."""
    if us <= 0:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    from datetime import timedelta

    unix_seconds = us / 1_000_000.0
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=unix_seconds)


def datetime_to_firefox_us(dt: datetime) -> int:
    """Convert UTC datetime to Firefox timestamp (microseconds since 1970-01-01)."""
    return int(dt.timestamp() * 1_000_000)


def parse_since_datetime(since: datetime | str | None) -> datetime | None:
    """Parse a since parameter into a timezone-aware UTC datetime."""
    if since is None:
        return None
    if isinstance(since, datetime):
        if since.tzinfo is None:
            return since.replace(tzinfo=timezone.utc)
        return since.astimezone(timezone.utc)
    if isinstance(since, str):
        # Handle ISO strings or date formats
        cleaned = since.strip()
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError:
            try:
                import dateutil.parser

                dt = dateutil.parser.parse(cleaned)
            except Exception as exc:
                raise ValueError(f"Invalid timestamp string for since: {since}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise TypeError(f"since must be datetime, str or None, got {type(since).__name__}")


# ---------------------------------------------------------------------------
# URL filtering, normalization, and redaction
# ---------------------------------------------------------------------------


def is_search_results_url(parsed: Any) -> bool:
    """Return True if URL points to a search engine query results page."""
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    query = parsed.query or ""

    for domain_pattern, search_path in _SEARCH_ENGINES:
        if domain_pattern in host:
            if domain_pattern == "duckduckgo.com":
                if path in ("", "/") and "q=" in query:
                    return True
            elif path.startswith(search_path):
                return True
    return False


def normalize_and_redact_url(
    raw_url: str,
    *,
    redact_sensitive_params: bool = True,
    drop_tracking_params: bool = True,
) -> str | None:
    """Normalize URL and strip sensitive or tracking query parameters.

    Returns None if URL contains credentials or has an unsupported scheme.
    """
    if not raw_url:
        return None
    try:
        parsed = urlparse(raw_url.strip())
    except Exception:
        return None

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return None

    # Credentials in URL (http://user:pass@host/)
    if parsed.username or parsed.password:
        return None

    host = (parsed.hostname or "").lower()
    if not host:
        return None

    port = parsed.port
    if port and ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        port = None
    netloc = f"{host}:{port}" if port else host

    path = parsed.path
    if not path:
        path = "/"
    elif len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Query params
    filtered_pairs = []
    if parsed.query:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        for k, v in pairs:
            k_lower = k.lower()
            if redact_sensitive_params and k_lower in _SENSITIVE_QUERY_PARAMS:
                continue
            if drop_tracking_params and k_lower in _TRACKING_QUERY_PARAMS:
                continue
            filtered_pairs.append((k, v))
        filtered_pairs.sort(key=lambda x: x[0])

    normalized_query = urlencode(filtered_pairs) if filtered_pairs else ""
    return urlunparse((scheme, netloc, path, "", normalized_query, ""))


def is_url_allowed(
    raw_url: str,
    *,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    drop_search_results: bool = True,
) -> bool:
    """Check whether a URL passes the built-in privacy list and user domain filters."""
    if not raw_url:
        return False
    try:
        parsed = urlparse(raw_url.strip())
    except Exception:
        return False

    scheme = parsed.scheme.lower()
    if scheme in _INTERNAL_SCHEMES or scheme not in ("http", "https"):
        return False

    if parsed.username or parsed.password:
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False

    # Skip localhost / loopback / private network domains
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(
        (".local", ".localhost", ".internal", ".lan", ".localdomain")
    ):
        return False

    # Skip built-in sensitive hosts (banking, webmail, auth/SSO)
    for sensitive_host in _SENSITIVE_HOSTS:
        if host == sensitive_host or host.endswith("." + sensitive_host):
            return False

    # Skip sensitive path segments (login, oauth, billing, etc.)
    path_segments = [seg.lower() for seg in parsed.path.split("/") if seg]
    for seg in path_segments:
        if seg in _SENSITIVE_PATH_KEYWORDS:
            return False

    # Skip search results pages
    if drop_search_results and is_search_results_url(parsed):
        return False

    # Check user include_domains
    if include_domains:
        matched = False
        for inc in include_domains:
            inc_clean = inc.lower().lstrip(".")
            if host == inc_clean or host.endswith("." + inc_clean):
                matched = True
                break
        if not matched:
            return False

    # Check user exclude_domains
    if exclude_domains:
        for exc in exclude_domains:
            exc_clean = exc.lower().lstrip(".")
            if host == exc_clean or host.endswith("." + exc_clean):
                return False

    return True


# ---------------------------------------------------------------------------
# Safe locked database access
# ---------------------------------------------------------------------------


@contextmanager
def copy_browser_db(db_path: Path) -> Iterator[Path]:
    """Copy a locked SQLite database and companion WAL/SHM files to a temp directory.

    Browsers hold an exclusive lock on history databases while open. Reading
    a temporary copy avoids lock contention. If copying or integrity check fails,
    an exception is raised so the run is aborted without pretending everything was deleted.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Browser database does not exist: {db_path}")

    with tempfile.TemporaryDirectory(prefix="cognee_browser_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        target_db = tmp_path / db_path.name
        try:
            shutil.copy2(db_path, target_db)
            wal_file = db_path.with_name(f"{db_path.name}-wal")
            if wal_file.exists():
                shutil.copy2(wal_file, tmp_path / wal_file.name)
            shm_file = db_path.with_name(f"{db_path.name}-shm")
            if shm_file.exists():
                shutil.copy2(shm_file, tmp_path / shm_file.name)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to copy browser database from {db_path} to temp directory: {exc}"
            ) from exc

        # Verify integrity and readability
        try:
            conn = sqlite3.connect(f"file:{target_db.as_posix()}?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA integrity_check(1);").fetchone()
            finally:
                conn.close()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to open or verify copied browser database at {target_db}: {exc}"
            ) from exc

        yield target_db


# ---------------------------------------------------------------------------
# Profile detection
# ---------------------------------------------------------------------------


def detect_browser_profile(
    browser: str = "auto",
    profile: str | None = None,
    profile_path: str | Path | None = None,
) -> tuple[str, Path]:
    """Detect browser family and resolve its profile directory.

    Returns:
        tuple of (browser_name, profile_directory_path)
    """
    if profile_path is not None:
        p = Path(profile_path).expanduser().resolve()
        if p.is_file():
            # If pointing directly to History or places.sqlite, use parent directory
            db_name = p.name.lower()
            detected = "firefox" if "places.sqlite" in db_name else "chrome"
            return (browser if browser != "auto" else detected, p.parent)
        if p.is_dir():
            if (p / "places.sqlite").exists():
                return (browser if browser != "auto" else "firefox", p)
            if (p / "History").exists():
                return (browser if browser != "auto" else "chrome", p)
            return (browser if browser != "auto" else "chrome", p)
        raise FileNotFoundError(f"Specified profile_path does not exist: {profile_path}")

    browser_clean = browser.lower().strip()
    candidates = (
        ["chrome", "edge", "brave", "vivaldi", "firefox"]
        if browser_clean == "auto"
        else [browser_clean]
    )

    platform = sys.platform
    home = Path.home()

    for cand in candidates:
        profile_dir = _find_candidate_profile(cand, platform, home, profile)
        if profile_dir is not None:
            return (cand, profile_dir)

    raise FileNotFoundError(
        f"Could not auto-detect browser profile for {browser!r} on {platform}. "
        "Please provide an explicit profile_path parameter."
    )


def _find_candidate_profile(
    browser: str, platform: str, home: Path, profile_name: str | None
) -> Path | None:
    profile_dir_name = profile_name or "Default"

    if browser == "chrome":
        if platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Google/Chrome/User Data"
        elif platform == "darwin":
            base = home / "Library/Application Support/Google/Chrome"
        else:
            base = home / ".config/google-chrome"
        target = base / profile_dir_name
        if target.exists() and (target / "History").exists():
            return target

    elif browser == "edge":
        if platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Microsoft/Edge/User Data"
        elif platform == "darwin":
            base = home / "Library/Application Support/Microsoft Edge"
        else:
            base = home / ".config/microsoft-edge"
        target = base / profile_dir_name
        if target.exists() and (target / "History").exists():
            return target

    elif browser == "brave":
        if platform == "win32":
            base = (
                Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
                / "BraveSoftware/Brave-Browser/User Data"
            )
        elif platform == "darwin":
            base = home / "Library/Application Support/BraveSoftware/Brave-Browser"
        else:
            base = home / ".config/BraveSoftware/Brave-Browser"
        target = base / profile_dir_name
        if target.exists() and (target / "History").exists():
            return target

    elif browser == "vivaldi":
        if platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Vivaldi/User Data"
        elif platform == "darwin":
            base = home / "Library/Application Support/Vivaldi"
        else:
            base = home / ".config/vivaldi"
        target = base / profile_dir_name
        if target.exists() and (target / "History").exists():
            return target

    elif browser == "firefox":
        if platform == "win32":
            base = Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "Mozilla/Firefox"
        elif platform == "darwin":
            base = home / "Library/Application Support/Firefox"
        else:
            base = home / ".mozilla/firefox"

        if not base.exists():
            return None

        # Check profiles.ini
        ini_file = base / "profiles.ini"
        if ini_file.exists():
            config = configparser.ConfigParser()
            try:
                config.read(ini_file, encoding="utf-8")
                # Look for requested profile or default
                for section in config.sections():
                    if section.startswith("Profile") or section.startswith("Install"):
                        sec_name = config.get(section, "Name", fallback="")
                        is_def = config.get(section, "Default", fallback="0") == "1"
                        path_val = config.get(section, "Path", fallback="")
                        is_rel = config.get(section, "IsRelative", fallback="1") == "1"

                        if profile_name and sec_name.lower() != profile_name.lower():
                            continue
                        if not profile_name and not is_def and not sec_name.startswith("default"):
                            continue

                        cand_path = base / path_val if is_rel else Path(path_val)
                        if cand_path.exists() and (cand_path / "places.sqlite").exists():
                            return cand_path
            except Exception:
                pass

        # Fallback: scan directory or Profiles/ for places.sqlite
        scan_dirs = [base, base / "Profiles"]
        for sdir in scan_dirs:
            if sdir.exists():
                for sub in sdir.iterdir():
                    if sub.is_dir() and (sub / "places.sqlite").exists():
                        if not profile_name or profile_name.lower() in sub.name.lower():
                            return sub

    return None


# ---------------------------------------------------------------------------
# Optional web page content fetching
# ---------------------------------------------------------------------------


def is_http_allowed() -> bool:
    """Check if outbound HTTP requests are allowed via ALLOW_HTTP_REQUESTS."""
    try:
        from cognee.tasks.web_scraper.ssrf_protection import is_http_requests_allowed

        return is_http_requests_allowed()
    except ImportError:
        val = os.getenv("ALLOW_HTTP_REQUESTS", "true").strip().lower()
        return val not in ("false", "0", "no", "off")


def fetch_page_text(url: str, timeout: int = 10) -> str | None:
    """Fetch URL and extract readable plain text, respecting ALLOW_HTTP_REQUESTS."""
    if not is_http_allowed():
        logger.warning(
            "fetch_content=True requested but ALLOW_HTTP_REQUESTS=false, skipping fetch for %s",
            url,
        )
        return None

    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "cognee-browser-connector/0.1.0"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            ctype = response.headers.get("Content-Type", "").lower()
            if "text/html" not in ctype and "text/plain" not in ctype:
                return None
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(1_000_000)  # limit to 1MB
            html_text = raw.decode(charset, errors="replace")
            return _clean_html_to_text(html_text)
    except Exception as exc:
        logger.debug("Failed to fetch content for %s: %s", url, exc)
        return None


def _clean_html_to_text(html_text: str) -> str:
    """Convert HTML content to clean, readable plain text."""
    import html

    # Remove script, style, noscript, nav, header, footer tags and contents
    text = _SCRIPT_STYLE_RE.sub(" ", html_text)
    # Strip HTML tags
    text = _TAG_RE.sub(" ", text)
    # Unescape HTML entities
    text = html.unescape(text)
    # Collapse multiple whitespaces
    lines = []
    for line in text.splitlines():
        line = _WS_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Bookmark extraction
# ---------------------------------------------------------------------------


def parse_chromium_bookmarks(bookmarks_path: Path) -> dict[str, dict[str, Any]]:
    """Parse Chromium Bookmarks JSON file into a mapping of normalized_url -> metadata."""
    if not bookmarks_path.exists():
        return {}

    try:
        with bookmarks_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("Could not read Chromium Bookmarks at %s: %s", bookmarks_path, exc)
        return {}

    results: dict[str, dict[str, Any]] = {}
    roots = data.get("roots", {})

    def _walk_folder(node: dict, current_path: str):
        node_type = node.get("type")
        if node_type == "url":
            raw_url = node.get("url", "")
            norm_url = normalize_and_redact_url(raw_url)
            if norm_url:
                date_added_raw = node.get("date_added", 0)
                try:
                    date_added = int(date_added_raw)
                except (ValueError, TypeError):
                    date_added = 0
                results[norm_url] = {
                    "folder": current_path,
                    "title": node.get("name", ""),
                    "date_added": date_added,
                    "raw_url": raw_url,
                }
        elif node_type == "folder":
            name = node.get("name", "")
            new_path = f"{current_path}/{name}" if current_path else name
            for child in node.get("children", []):
                _walk_folder(child, new_path)

    for root_key, root_node in roots.items():
        if isinstance(root_node, dict):
            root_name = root_node.get("name", root_key)
            for child in root_node.get("children", []):
                _walk_folder(child, root_name)

    return results


def parse_firefox_bookmarks(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Parse bookmarks and folder hierarchies from Firefox places.sqlite connection."""
    # Build folder id -> name map and parent mapping
    cur = conn.cursor()
    folders: dict[int, dict[str, Any]] = {}
    try:
        cur.execute("SELECT id, title, parent FROM moz_bookmarks WHERE type = 2")
        for f_id, title, parent in cur.fetchall():
            folders[f_id] = {"title": title or "", "parent": parent}
    except Exception as exc:
        logger.debug("Failed to query moz_bookmarks folders: %s", exc)
        return {}

    def _build_folder_path(parent_id: int) -> str:
        parts = []
        curr = parent_id
        visited = set()
        while curr in folders and curr not in visited:
            visited.add(curr)
            f_title = folders[curr]["title"]
            # Skip root container names if generic
            if f_title and f_title not in ("root", "PlacesRoot"):
                parts.append(f_title)
            curr = folders[curr]["parent"]
        parts.reverse()
        return "/".join(parts)

    results: dict[str, dict[str, Any]] = {}
    try:
        cur.execute(
            """
            SELECT b.fk, b.title, b.parent, b.dateAdded, p.url
            FROM moz_bookmarks b
            JOIN moz_places p ON b.fk = p.id
            WHERE b.type = 1
            """
        )
        for fk, title, parent, date_added, raw_url in cur.fetchall():
            norm_url = normalize_and_redact_url(raw_url)
            if norm_url:
                folder_path = _build_folder_path(parent)
                results[norm_url] = {
                    "folder": folder_path,
                    "title": title or "",
                    "date_added": date_added or 0,
                    "place_id": fk,
                    "raw_url": raw_url,
                }
    except Exception as exc:
        logger.debug("Failed to query moz_bookmarks entries: %s", exc)

    return results


# ---------------------------------------------------------------------------
# Row formatting and ID derivation
# ---------------------------------------------------------------------------


def generate_row_id(browser: str, profile: str, normalized_url: str) -> str:
    """Generate a stable 32-character hash row id for a page."""
    key = f"{browser}:{profile}:{normalized_url}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:32]


def build_page_row(
    *,
    row_id: str,
    normalized_url: str,
    title: str,
    domain: str,
    browser: str,
    profile: str,
    visit_count: int,
    first_visit_dt: datetime | None,
    last_visit_dt: datetime | None,
    is_bookmarked: bool,
    bookmark_folder: str | None,
    page_text: str | None = None,
) -> dict[str, Any]:
    """Build a document-mode row for the dlt resource."""
    # Compose document content
    content_lines = [
        f"# {title}" if title else f"# {normalized_url}",
        "",
        f"- URL: {normalized_url}",
        f"- Domain: {domain}",
        f"- Browser: {browser}",
        f"- Profile: {profile}",
        f"- Visits: {visit_count}",
    ]
    if first_visit_dt:
        content_lines.append(f"- First visited: {first_visit_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if last_visit_dt:
        content_lines.append(f"- Last visited: {last_visit_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if is_bookmarked:
        folder_suffix = f" (Folder: {bookmark_folder})" if bookmark_folder else ""
        content_lines.append(f"- Bookmarked: Yes{folder_suffix}")

    if page_text:
        content_lines.extend(["", "---", "", page_text])

    document_content = "\n".join(content_lines)

    # Node sets
    node_sets = [
        f"browser:{browser}",
        f"browser:{domain}",
    ]
    if is_bookmarked:
        node_sets.append("browser:bookmarks")
        if bookmark_folder:
            clean_folder = bookmark_folder.strip("/")
            if clean_folder:
                node_sets.append(f"browser:bookmarks/{clean_folder}")

    return {
        "id": row_id,
        "url": normalized_url,
        "title": title or normalized_url,
        "content": document_content,
        "domain": domain,
        "browser": browser,
        "profile": profile,
        "visit_count": visit_count,
        "first_visit_time": first_visit_dt.isoformat() if first_visit_dt else None,
        "last_visit_time": last_visit_dt.isoformat() if last_visit_dt else None,
        "is_bookmarked": is_bookmarked,
        "bookmark_folder": bookmark_folder,
        NODE_SET_COLUMN: node_sets,
        "_deleted": False,
    }


def deleted_page_row(row_id: str) -> dict[str, Any]:
    """Emit a hard-delete tombstone row."""
    return {"id": row_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------


def sync_browser_pages(
    browser: str,
    profile_path: Path,
    resource_state: dict[str, Any],
    *,
    profile_name: str = "default",
    since: datetime | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    min_visits: int = 1,
    bookmarks_only: bool = False,
    fetch_content: bool = False,
    redact_urls: bool = True,
    drop_search_results: bool = True,
    drop_redirects: bool = True,
) -> Iterator[dict[str, Any]]:
    """Read browser history and bookmarks, yielding active rows and deletion tombstones."""
    is_firefox = browser == "firefox" or (profile_path / "places.sqlite").exists()
    db_file = profile_path / ("places.sqlite" if is_firefox else "History")

    if not db_file.exists():
        raise FileNotFoundError(
            f"Browser database {db_file.name} not found in profile directory: {profile_path}"
        )

    last_cursor_us = int(resource_state.get("last_visit_time", 0))
    known_ids = set(resource_state.get("known_ids", []))

    # Calculate cutoff from 'since' parameter
    since_dt = parse_since_datetime(since)
    since_cutoff_us = 0
    if since_dt is not None:
        since_cutoff_us = (
            datetime_to_firefox_us(since_dt) if is_firefox else datetime_to_chromium_us(since_dt)
        )

    # Use a safe temp copy to prevent locking
    with copy_browser_db(db_file) as copied_db:
        conn = sqlite3.connect(f"file:{copied_db.as_posix()}?mode=ro", uri=True)
        try:
            if is_firefox:
                yield from _sync_firefox(
                    conn=conn,
                    browser=browser,
                    profile_name=profile_name,
                    last_cursor_us=last_cursor_us,
                    since_cutoff_us=since_cutoff_us,
                    known_ids=known_ids,
                    resource_state=resource_state,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    min_visits=min_visits,
                    bookmarks_only=bookmarks_only,
                    fetch_content=fetch_content,
                    redact_urls=redact_urls,
                    drop_search_results=drop_search_results,
                    drop_redirects=drop_redirects,
                )
            else:
                bookmarks_file = profile_path / "Bookmarks"
                bookmarks_map = (
                    parse_chromium_bookmarks(bookmarks_file) if bookmarks_file.exists() else {}
                )
                yield from _sync_chromium(
                    conn=conn,
                    bookmarks_map=bookmarks_map,
                    browser=browser,
                    profile_name=profile_name,
                    last_cursor_us=last_cursor_us,
                    since_cutoff_us=since_cutoff_us,
                    known_ids=known_ids,
                    resource_state=resource_state,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    min_visits=min_visits,
                    bookmarks_only=bookmarks_only,
                    fetch_content=fetch_content,
                    redact_urls=redact_urls,
                    drop_search_results=drop_search_results,
                    drop_redirects=drop_redirects,
                )
        finally:
            conn.close()


def _sync_chromium(
    conn: sqlite3.Connection,
    bookmarks_map: dict[str, dict[str, Any]],
    browser: str,
    profile_name: str,
    last_cursor_us: int,
    since_cutoff_us: int,
    known_ids: set[str],
    resource_state: dict[str, Any],
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    min_visits: int,
    bookmarks_only: bool,
    fetch_content: bool,
    redact_urls: bool,
    drop_search_results: bool,
    drop_redirects: bool,
) -> Iterator[dict[str, Any]]:
    cur = conn.cursor()

    # Query all candidate URLs from Chromium urls table
    cur.execute(
        """
        SELECT u.id, u.url, u.title, u.visit_count, u.last_visit_time, u.hidden,
               MIN(v.visit_time) AS first_visit_time,
               MAX(v.visit_time) AS max_visit_time,
               GROUP_CONCAT(v.transition) AS transitions
        FROM urls u
        LEFT JOIN visits v ON u.id = v.url
        GROUP BY u.id
        """
    )
    rows = cur.fetchall()

    current_ids: set[str] = set()
    items_to_emit: list[dict[str, Any]] = []
    max_ts_seen = last_cursor_us

    # Track URLs present in History table
    history_urls_seen: set[str] = set()

    for (
        _,
        raw_url,
        title,
        visit_count,
        last_visit_time,
        hidden,
        first_visit_time,
        max_visit_time,
        transitions_str,
    ) in rows:
        if not is_url_allowed(
            raw_url,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            drop_search_results=drop_search_results,
        ):
            continue

        norm_url = normalize_and_redact_url(raw_url, redact_sensitive_params=redact_urls)
        if not norm_url:
            continue

        history_urls_seen.add(norm_url)

        # Check redirect filtering
        if drop_redirects:
            if hidden == 1:
                continue
            if transitions_str:
                transitions = [int(t) for t in transitions_str.split(",") if t.isdigit()]
                # If all visits are redirect transitions (0x40000000 or 0x80000000)
                if transitions and all((t & 0xC0000000) != 0 for t in transitions):
                    continue

        bookmark_info = bookmarks_map.get(norm_url)
        is_bookmarked = bookmark_info is not None
        bookmark_folder = bookmark_info["folder"] if bookmark_info else None
        if is_bookmarked and not title:
            title = bookmark_info.get("title", "")

        if bookmarks_only and not is_bookmarked:
            continue

        # Effective visit count
        v_count = int(visit_count or 0)
        if not is_bookmarked and v_count < min_visits:
            continue

        # Timestamps
        last_ts = int(max_visit_time or last_visit_time or 0)
        first_ts = int(first_visit_time or last_ts)

        # If bookmarked, date_added may be newer
        if is_bookmarked and bookmark_info.get("date_added"):
            added_ts = int(bookmark_info["date_added"])
            if added_ts > last_ts:
                last_ts = added_ts
            if first_ts == 0:
                first_ts = added_ts

        if since_cutoff_us > 0 and last_ts < since_cutoff_us:
            continue

        row_id = generate_row_id(browser, profile_name, norm_url)
        current_ids.add(row_id)

        if last_ts > max_ts_seen:
            max_ts_seen = last_ts

        # Determine if this row is new or updated since last cursor
        # If last_cursor_us == 0 (first run), emit everything matching criteria
        if last_cursor_us == 0 or last_ts > last_cursor_us:
            domain = urlparse(norm_url).hostname or ""
            first_dt = chromium_us_to_datetime(first_ts) if first_ts > 0 else None
            last_dt = chromium_us_to_datetime(last_ts) if last_ts > 0 else None

            page_text = fetch_page_text(norm_url) if fetch_content else None
            row = build_page_row(
                row_id=row_id,
                normalized_url=norm_url,
                title=title or norm_url,
                domain=domain,
                browser=browser,
                profile=profile_name,
                visit_count=v_count,
                first_visit_dt=first_dt,
                last_visit_dt=last_dt,
                is_bookmarked=is_bookmarked,
                bookmark_folder=bookmark_folder,
                page_text=page_text,
            )
            items_to_emit.append(row)

    # Ingest bookmarked URLs that have 0 visits in History (or were cleared from history)
    for norm_url, binfo in bookmarks_map.items():
        if norm_url in history_urls_seen:
            continue
        raw_b_url = binfo.get("raw_url", norm_url)
        if not is_url_allowed(
            raw_b_url,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            drop_search_results=drop_search_results,
        ):
            continue

        added_ts = int(binfo.get("date_added", 0))
        if since_cutoff_us > 0 and added_ts < since_cutoff_us:
            continue

        row_id = generate_row_id(browser, profile_name, norm_url)
        current_ids.add(row_id)

        if added_ts > max_ts_seen:
            max_ts_seen = added_ts

        if last_cursor_us == 0 or added_ts > last_cursor_us:
            domain = urlparse(norm_url).hostname or ""
            dt_added = chromium_us_to_datetime(added_ts) if added_ts > 0 else None
            page_text = fetch_page_text(norm_url) if fetch_content else None
            row = build_page_row(
                row_id=row_id,
                normalized_url=norm_url,
                title=binfo.get("title") or norm_url,
                domain=domain,
                browser=browser,
                profile=profile_name,
                visit_count=0,
                first_visit_dt=dt_added,
                last_visit_dt=dt_added,
                is_bookmarked=True,
                bookmark_folder=binfo.get("folder"),
                page_text=page_text,
            )
            items_to_emit.append(row)

    # Forget-on-delete: emit tombstones for disappeared IDs
    deleted_ids = known_ids - current_ids
    for del_id in sorted(deleted_ids):
        yield deleted_page_row(del_id)

    # Emit active items
    for item in items_to_emit:
        yield item

    # Update resource state
    resource_state["known_ids"] = sorted(current_ids)
    resource_state["last_visit_time"] = max_ts_seen
    logger.info(
        "Browser (%s/%s): %d emitted, %d hard-delete tombstones.",
        browser,
        profile_name,
        len(items_to_emit),
        len(deleted_ids),
    )


def _sync_firefox(
    conn: sqlite3.Connection,
    browser: str,
    profile_name: str,
    last_cursor_us: int,
    since_cutoff_us: int,
    known_ids: set[str],
    resource_state: dict[str, Any],
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    min_visits: int,
    bookmarks_only: bool,
    fetch_content: bool,
    redact_urls: bool,
    drop_search_results: bool,
    drop_redirects: bool,
) -> Iterator[dict[str, Any]]:
    bookmarks_map = parse_firefox_bookmarks(conn)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT p.id, p.url, p.title, p.visit_count, p.last_visit_date, p.hidden,
               MIN(v.visit_date) AS first_visit_date,
               MAX(v.visit_date) AS max_visit_date,
               GROUP_CONCAT(v.visit_type) AS visit_types
        FROM moz_places p
        LEFT JOIN moz_historyvisits v ON p.id = v.place_id
        GROUP BY p.id
        """
    )
    rows = cur.fetchall()

    current_ids: set[str] = set()
    items_to_emit: list[dict[str, Any]] = []
    max_ts_seen = last_cursor_us

    for (
        _,
        raw_url,
        title,
        visit_count,
        last_visit_date,
        hidden,
        first_visit_date,
        max_visit_date,
        visit_types_str,
    ) in rows:
        if not is_url_allowed(
            raw_url,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            drop_search_results=drop_search_results,
        ):
            continue

        norm_url = normalize_and_redact_url(raw_url, redact_sensitive_params=redact_urls)
        if not norm_url:
            continue

        # Check redirect filtering
        if drop_redirects:
            if hidden == 1:
                continue
            if visit_types_str:
                types = [int(t) for t in visit_types_str.split(",") if t.isdigit()]
                # Firefox transition 5 = PERMANENT_REDIRECT, 6 = TEMPORARY_REDIRECT
                if types and all(t in (5, 6) for t in types):
                    continue

        bookmark_info = bookmarks_map.get(norm_url)
        is_bookmarked = bookmark_info is not None
        bookmark_folder = bookmark_info["folder"] if bookmark_info else None
        if is_bookmarked and not title:
            title = bookmark_info.get("title", "")

        if bookmarks_only and not is_bookmarked:
            continue

        v_count = int(visit_count or 0)
        if not is_bookmarked and v_count < min_visits:
            continue

        last_ts = int(max_visit_date or last_visit_date or 0)
        first_ts = int(first_visit_date or last_ts)

        if is_bookmarked and bookmark_info.get("date_added"):
            added_ts = int(bookmark_info["date_added"])
            if added_ts > last_ts:
                last_ts = added_ts
            if first_ts == 0:
                first_ts = added_ts

        if since_cutoff_us > 0 and last_ts < since_cutoff_us:
            continue

        row_id = generate_row_id(browser, profile_name, norm_url)
        current_ids.add(row_id)

        if last_ts > max_ts_seen:
            max_ts_seen = last_ts

        if last_cursor_us == 0 or last_ts > last_cursor_us:
            domain = urlparse(norm_url).hostname or ""
            first_dt = firefox_us_to_datetime(first_ts) if first_ts > 0 else None
            last_dt = firefox_us_to_datetime(last_ts) if last_ts > 0 else None

            page_text = fetch_page_text(norm_url) if fetch_content else None
            row = build_page_row(
                row_id=row_id,
                normalized_url=norm_url,
                title=title or norm_url,
                domain=domain,
                browser=browser,
                profile=profile_name,
                visit_count=v_count,
                first_visit_dt=first_dt,
                last_visit_dt=last_dt,
                is_bookmarked=is_bookmarked,
                bookmark_folder=bookmark_folder,
                page_text=page_text,
            )
            items_to_emit.append(row)

    # Tombstones for deleted pages
    deleted_ids = known_ids - current_ids
    for del_id in sorted(deleted_ids):
        yield deleted_page_row(del_id)

    # Active items
    for item in items_to_emit:
        yield item

    resource_state["known_ids"] = sorted(current_ids)
    resource_state["last_visit_time"] = max_ts_seen
    logger.info(
        "Firefox (%s/%s): %d emitted, %d hard-delete tombstones.",
        browser,
        profile_name,
        len(items_to_emit),
        len(deleted_ids),
    )


# ---------------------------------------------------------------------------
# Public dlt source factory
# ---------------------------------------------------------------------------


def browser_source(
    *,
    browser: str = "auto",
    profile_path: str | Path | None = None,
    profile: str | None = None,
    since: datetime | str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    min_visits: int = 1,
    bookmarks_only: bool = False,
    fetch_content: bool = False,
    redact_urls: bool = True,
    drop_search_results: bool = True,
    drop_redirects: bool = True,
) -> Any:
    """Create a ``dlt`` resource that yields browser history and bookmarks as documents.

    Args:
        browser: Target browser ("chrome", "edge", "brave", "vivaldi", "firefox", or "auto").
        profile_path: Explicit path to the browser profile directory or history database.
        profile: Profile folder name (e.g. "Default", "Profile 1") when auto-detecting.
        since: Only ingest pages visited or bookmarked after this timestamp (datetime or ISO str).
        include_domains: Restrict ingestion to these domain names (e.g. ["docs.python.org"]).
        exclude_domains: Skip pages from these domain names.
        min_visits: Minimum visit count required to ingest a page (default 1).
        bookmarks_only: Only ingest bookmarked pages.
        fetch_content: Fetch page readable text over HTTP (default False; opt-in).
        redact_urls: Strip sensitive query parameters (tokens, keys, auth codes).
        drop_search_results: Drop search engine result pages (Google, Bing, DDG, etc.).
        drop_redirects: Filter out internal redirects and subframe hops.

    Returns:
        A ``dlt`` resource configured with merge disposition, primary key "id",
        and "_deleted" hard-delete marker, tagged for document-mode ingestion in cognee.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The browser connector requires dlt: pip install "dlt[sqlalchemy]>=1.9.0"'
        ) from exc

    resolved_browser, resolved_profile_path = detect_browser_profile(
        browser=browser,
        profile=profile,
        profile_path=profile_path,
    )

    profile_name = profile or resolved_profile_path.name
    since_dt = parse_since_datetime(since)
    pipeline_scope = f"browser:{resolved_browser}:{profile_name}"

    @dlt.resource(
        name=BROWSER_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            NODE_SET_COLUMN: {"data_type": "json", "nullable": True},
            "bookmark_folder": {"data_type": "text", "nullable": True},
        },
    )
    def browser_pages():
        resource_state = dlt.current.resource_state()
        yield from sync_browser_pages(
            browser=resolved_browser,
            profile_path=resolved_profile_path,
            resource_state=resource_state,
            profile_name=profile_name,
            since=since_dt,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            min_visits=min_visits,
            bookmarks_only=bookmarks_only,
            fetch_content=fetch_content,
            redact_urls=redact_urls,
            drop_search_results=drop_search_results,
            drop_redirects=drop_redirects,
        )

    setattr(browser_pages, DOCUMENT_SOURCE_ATTR, BROWSER_SOURCE_NAME)
    setattr(browser_pages, PIPELINE_SCOPE_ATTR, pipeline_scope)
    return browser_pages

