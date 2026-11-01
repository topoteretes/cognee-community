"""A deterministic sample monorepo, used by tests and the CLI demo.

The repo is a tiny shop whose call chain runs ``verify_token`` <- ``load_session`` <-
``get_cart`` <- ``checkout`` <- ``send_receipt`` (``charge`` calls ``verify_token`` directly,
``search`` never touches auth). Every commit uses a fixed author and date, so the SHAs are
identical on every run and every machine.
"""

from dataclasses import dataclass
from pathlib import Path

from git import Actor, Repo

_ACTOR = Actor("Ada Dev", "ada@example.com")

_AUTH_V1 = '''"""Token helpers."""


def make_token(user_id):
    return f"tok-{user_id}"


def verify_token(token):
    return token.startswith("tok-")
'''

_AUTH_V2 = '''"""Token helpers."""


def make_token(user_id):
    return f"tok-{user_id}"


def verify_token(token, max_age=3600):
    return token.startswith("tok-") and max_age > 0
'''

_AUTH_ROTATED = '''"""Token helpers."""


def make_token(user_id):
    return f"tok-{user_id}"


def verify_token(token, max_age=3600):
    key_id = token.split("-")[0]
    return key_id == "tok" and max_age > 0
'''

_INVOICE_V1 = '''"""Invoices."""

from shared.auth import verify_token


def charge(token, amount):
    if not verify_token(token):
        raise PermissionError("bad token")
    return {"token": token, "amount": amount}
'''

_INVOICE_V2 = '''"""Invoices."""

from shared.auth import verify_token


def charge(token, amount, max_age=3600):
    if not verify_token(token, max_age):
        raise PermissionError("bad token")
    return {"token": token, "amount": amount}
'''

_Files = dict[str, str]

_COMMITS: list[tuple[str, _Files]] = [
    (
        "add shared auth",
        {"shared/__init__.py": "", "shared/auth.py": _AUTH_V1},
    ),
    (
        "add billing",
        {"billing/__init__.py": "", "billing/invoice.py": _INVOICE_V1},
    ),
    (
        "add session and cart",
        {
            "shared/session.py": (
                '"""Sessions."""\n\n'
                "from shared.auth import verify_token\n\n\n"
                "def load_session(token):\n"
                '    return {"token": token, "valid": verify_token(token)}\n'
            ),
            "orders/__init__.py": "",
            "orders/cart.py": (
                '"""Carts."""\n\n'
                "from shared.session import load_session\n\n\n"
                "def get_cart(token):\n"
                '    return {"session": load_session(token), "items": []}\n'
            ),
        },
    ),
    (
        "add checkout",
        {
            "orders/checkout.py": (
                '"""Checkout."""\n\n'
                "from orders.cart import get_cart\n\n\n"
                "def checkout(token):\n"
                "    return get_cart(token)\n"
            ),
        },
    ),
    (
        "add receipts",
        {
            "notifications/__init__.py": "",
            "notifications/receipts.py": (
                '"""Receipts."""\n\n'
                "from orders.checkout import checkout\n\n\n"
                "def send_receipt(token):\n"
                "    return checkout(token)\n"
            ),
        },
    ),
    (
        "add search",
        {
            "search/__init__.py": "",
            "search/index.py": (
                '"""Product search."""\n\n\n'
                "def search(query):\n"
                "    return [word for word in query.split() if word]\n"
            ),
        },
    ),
    (
        "add token expiry",
        {"shared/auth.py": _AUTH_V2, "billing/invoice.py": _INVOICE_V2},
    ),
]


@dataclass(frozen=True)
class SampleRepo:
    path: Path
    base: str = "main"
    head: str = "feature/rotate-tokens"


def _commit(repo: Repo, root: Path, index: int, message: str, files: _Files) -> None:
    for name, content in files.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("w", newline="\n") as handle:
            handle.write(content)
    repo.index.add(list(files))
    when = f"2024-01-{index + 1:02d}T12:00:00+0000"
    repo.index.commit(
        message,
        author=_ACTOR,
        committer=_ACTOR,
        author_date=when,
        commit_date=when,
        skip_hooks=True,
    )


def build_sample_repo(path: str | Path) -> SampleRepo:
    """Build the sample monorepo at ``path``, which must be missing or empty."""
    root = Path(path)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise FileExistsError(f"{root} already exists and is not an empty directory")
    root.mkdir(parents=True, exist_ok=True)
    sample = SampleRepo(root)
    repo = Repo.init(root, initial_branch=sample.base)
    for index, (message, files) in enumerate(_COMMITS):
        _commit(repo, root, index, message, files)
    repo.create_head(sample.head).checkout()
    _commit(repo, root, len(_COMMITS), "rotate token keys", {"shared/auth.py": _AUTH_ROTATED})
    repo.heads[sample.base].checkout()
    return sample
