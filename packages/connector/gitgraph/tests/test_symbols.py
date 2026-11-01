"""Symbol and reference rows read from the git tree at a ref."""

import random
import string
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from cognee_community_connector_gitgraph import SampleRepo, build_sample_repo
from cognee_community_connector_gitgraph.symbols import extract_symbols, parse_module

FEATURE = "feature/rotate-tokens"


@pytest.fixture
def sample(tmp_path: Path) -> SampleRepo:
    return build_sample_repo(tmp_path / "repo")


def _commit_files(repo: Repo, root: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content if isinstance(content, bytes) else content.encode())
    repo.index.add(list(files))
    repo.index.commit("c")


def _extract(tmp_path: Path, files: dict[str, str | bytes]) -> dict[str, list[dict[str, Any]]]:
    repo = Repo.init(tmp_path, initial_branch="main")
    _commit_files(repo, tmp_path, files)
    return extract_symbols(tmp_path, "main")


def _edges(out: dict[str, list[dict[str, Any]]]) -> set[tuple[str, str | None, str, str]]:
    return {
        (r["from_file"], r["from_symbol"], r["to_symbol"], r["kind"]) for r in out["references"]
    }


def test_sample_symbols_at_main(sample: SampleRepo) -> None:
    symbols = extract_symbols(sample.path, "main")["symbols"]
    assert [s["id"] for s in symbols] == [
        "billing/invoice.py::charge",
        "notifications/receipts.py::send_receipt",
        "orders/cart.py::get_cart",
        "orders/checkout.py::checkout",
        "search/index.py::search",
        "shared/auth.py::make_token",
        "shared/auth.py::verify_token",
        "shared/session.py::load_session",
    ]
    assert {s["kind"] for s in symbols} == {"function"}
    by_id = {s["id"]: s for s in symbols}
    assert by_id["shared/auth.py::make_token"] == {
        "id": "shared/auth.py::make_token",
        "kind": "function",
        "name": "make_token",
        "file": "shared/auth.py",
        "line": 4,
        "end_line": 5,
    }
    assert by_id["shared/auth.py::verify_token"]["line"] == 8
    assert by_id["search/index.py::search"]["line"] == 4


def test_sample_references_at_main(sample: SampleRepo) -> None:
    out = extract_symbols(sample.path, "main")
    verify = "shared/auth.py::verify_token"
    assert _edges(out) == {
        ("billing/invoice.py", None, verify, "import"),
        ("shared/session.py", None, verify, "import"),
        ("orders/cart.py", None, "shared/session.py::load_session", "import"),
        ("orders/checkout.py", None, "orders/cart.py::get_cart", "import"),
        ("notifications/receipts.py", None, "orders/checkout.py::checkout", "import"),
        ("billing/invoice.py", "billing/invoice.py::charge", verify, "call"),
        ("shared/session.py", "shared/session.py::load_session", verify, "call"),
        ("orders/cart.py", "orders/cart.py::get_cart", "shared/session.py::load_session", "call"),
        (
            "orders/checkout.py",
            "orders/checkout.py::checkout",
            "orders/cart.py::get_cart",
            "call",
        ),
        (
            "notifications/receipts.py",
            "notifications/receipts.py::send_receipt",
            "orders/checkout.py::checkout",
            "call",
        ),
    }
    assert len(out["references"]) == 10
    assert out["skipped"] == []
    first = out["references"][0]
    expected = f"{first['from_file']}:{first['from_symbol'] or ''}->{first['to_symbol']}"
    assert first["id"] == f"{expected}:{first['kind']}"
    assert [r["id"] for r in out["references"]] == sorted(r["id"] for r in out["references"])


def test_ref_matters(sample: SampleRepo) -> None:
    out = extract_symbols(sample.path, FEATURE)
    assert "shared/auth.py::verify_token" in {s["id"] for s in out["symbols"]}


def test_deleted_file_absent_at_later_ref(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path, initial_branch="main")
    _commit_files(repo, tmp_path, {"a.py": "def f():\n    pass\n", "b.py": "def g():\n    pass\n"})
    first = repo.head.commit.hexsha
    repo.index.remove(["a.py"], working_tree=True)
    repo.index.commit("drop a")
    assert [s["id"] for s in extract_symbols(tmp_path, "main")["symbols"]] == ["b.py::g"]
    assert [s["id"] for s in extract_symbols(tmp_path, first)["symbols"]] == ["a.py::f", "b.py::g"]


def test_reads_tree_not_working_dir(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path, initial_branch="main")
    _commit_files(repo, tmp_path, {"a.py": "def f():\n    pass\n"})
    (tmp_path / "a.py").write_text("def other():\n    pass\n")
    assert [s["id"] for s in extract_symbols(tmp_path, "main")["symbols"]] == ["a.py::f"]


def test_methods_async_and_nested_skipped(tmp_path: Path) -> None:
    src = (
        "class A:\n"
        "    def m(self):\n"
        "        def inner():\n"
        "            pass\n"
        "    class Inner:\n"
        "        def x(self):\n"
        "            pass\n"
        "\n"
        "async def go():\n"
        "    def nested():\n"
        "        pass\n"
    )
    symbols = _extract(tmp_path, {"m.py": src})["symbols"]
    assert [(s["id"], s["kind"], s["name"], s["line"]) for s in symbols] == [
        ("m.py::A", "class", "A", 1),
        ("m.py::A.m", "method", "A.m", 2),
        ("m.py::go", "function", "go", 9),
    ]


def test_relative_imports(tmp_path: Path) -> None:
    out = _extract(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/auth.py": "def verify():\n    pass\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/a.py": "from ..auth import verify\n\ndef run():\n    verify()\n",
            "pkg/b.py": "from .auth import verify as v\n\ndef run():\n    v()\n",
        },
    )
    assert _edges(out) == {
        ("pkg/sub/a.py", None, "pkg/auth.py::verify", "import"),
        ("pkg/sub/a.py", "pkg/sub/a.py::run", "pkg/auth.py::verify", "call"),
        ("pkg/b.py", None, "pkg/auth.py::verify", "import"),
        ("pkg/b.py", "pkg/b.py::run", "pkg/auth.py::verify", "call"),
    }


def test_relative_import_above_root_ignored(tmp_path: Path) -> None:
    out = _extract(
        tmp_path,
        {"a.py": "def f():\n    pass\n", "pkg/__init__.py": "", "pkg/b.py": "from ... import f\n"},
    )
    assert out["references"] == []
    out2 = _extract(tmp_path / "x", {"a.py": "def f():\n    pass\n", "b.py": "from .. import f\n"})
    assert out2["references"] == []


def test_module_alias_calls(tmp_path: Path) -> None:
    out = _extract(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "def f():\n    pass\n",
            "one.py": "import pkg.mod as m\n\ndef a():\n    m.f()\n",
            "two.py": "from pkg import mod\n\ndef b():\n    mod.f()\n",
            "three.py": "import pkg.mod\n\ndef c():\n    pkg.mod.f()\n",
        },
    )
    target = "pkg/mod.py::f"
    assert _edges(out) == {
        ("one.py", "one.py::a", target, "call"),
        ("two.py", "two.py::b", target, "call"),
        ("three.py", "three.py::c", target, "call"),
    }


def test_alias_and_same_module_calls(tmp_path: Path) -> None:
    out = _extract(
        tmp_path,
        {
            "a.py": "def f():\n    pass\n",
            "b.py": (
                "from a import f as g\n"
                "\n"
                "def helper():\n"
                "    pass\n"
                "\n"
                "def run():\n"
                "    g()\n"
                "    helper()\n"
                "    print(1)\n"
                "    unknown()\n"
                "    obj.method()\n"
                "\n"
                "class K:\n"
                "    def m(self):\n"
                "        helper()\n"
                "\n"
                "g()\n"
            ),
        },
    )
    assert _edges(out) == {
        ("b.py", None, "a.py::f", "import"),
        ("b.py", "b.py::run", "a.py::f", "call"),
        ("b.py", "b.py::run", "b.py::helper", "call"),
        ("b.py", "b.py::K.m", "b.py::helper", "call"),
        ("b.py", None, "a.py::f", "call"),
    }


def test_self_and_cls_method_calls(tmp_path: Path) -> None:
    src = (
        "class C:\n"
        "    def a(self):\n"
        "        self.b()\n"
        "        self.missing()\n"
        "    def b(self):\n"
        "        pass\n"
        "    def c(self):\n"
        "        def inner():\n"
        "            self.b()\n"
        "    @classmethod\n"
        "    def make(cls):\n"
        "        cls.b()\n"
        "\n"
        "C.b()\n"
    )
    out = _extract(tmp_path, {"m.py": src})
    assert _edges(out) == {
        ("m.py", "m.py::C.a", "m.py::C.b", "call"),
        ("m.py", "m.py::C.c", "m.py::C.b", "call"),
        ("m.py", "m.py::C.make", "m.py::C.b", "call"),
        ("m.py", None, "m.py::C.b", "call"),
    }


def test_instance_method_calls_across_files(tmp_path: Path) -> None:
    out = _extract(
        tmp_path,
        {
            "shop/__init__.py": "",
            "shop/cart.py": "class Cart:\n    def total(self):\n        pass\n",
            "run.py": (
                "from shop.cart import Cart\n"
                "import shop.cart as sc\n"
                "\n"
                "def go():\n"
                "    c = Cart()\n"
                "    c.total()\n"
                "\n"
                "def go2():\n"
                "    d = sc.Cart()\n"
                "    d.total()\n"
                "    sc.Cart.total()\n"
                "\n"
                "cart = Cart()\n"
                "cart.total()\n"
            ),
        },
    )
    cart, total = "shop/cart.py::Cart", "shop/cart.py::Cart.total"
    assert _edges(out) == {
        ("run.py", None, cart, "import"),
        ("run.py", "run.py::go", cart, "call"),
        ("run.py", "run.py::go", total, "call"),
        ("run.py", "run.py::go2", cart, "call"),
        ("run.py", "run.py::go2", total, "call"),
        ("run.py", None, cart, "call"),
        ("run.py", None, total, "call"),
    }


def test_local_class_instance_method_call(tmp_path: Path) -> None:
    src = (
        "class Repo:\n"
        "    def save(self):\n"
        "        pass\n"
        "\n"
        "def run():\n"
        "    r = Repo()\n"
        "    r.save()\n"
    )
    out = _extract(tmp_path, {"m.py": src})
    assert _edges(out) == {
        ("m.py", "m.py::run", "m.py::Repo", "call"),
        ("m.py", "m.py::run", "m.py::Repo.save", "call"),
    }


def test_ambiguous_instance_not_resolved(tmp_path: Path) -> None:
    src = (
        "class A:\n"
        "    def m(self):\n"
        "        pass\n"
        "\n"
        "class B:\n"
        "    def m(self):\n"
        "        pass\n"
        "\n"
        "def run(flag):\n"
        "    x = A()\n"
        "    if flag:\n"
        "        x = B()\n"
        "    x.m()\n"
        "\n"
        "def other(make):\n"
        "    y = A()\n"
        "    y = make()\n"
        "    y.m()\n"
        "\n"
        "def third():\n"
        "    for z in []:\n"
        "        pass\n"
        "    z = A()\n"
        "    z.m()\n"
    )
    out = _extract(tmp_path, {"m.py": src})
    assert _edges(out) == {
        ("m.py", "m.py::run", "m.py::A", "call"),
        ("m.py", "m.py::run", "m.py::B", "call"),
        ("m.py", "m.py::other", "m.py::A", "call"),
        ("m.py", "m.py::third", "m.py::A", "call"),
    }


def test_unrelated_attribute_calls_stay_unresolved(tmp_path: Path) -> None:
    out = _extract(
        tmp_path,
        {
            "a.py": "class A:\n    def m(self):\n        pass\n",
            "b.py": (
                "from a import A\n"
                "\n"
                "cart = A()\n"
                "\n"
                "def f():\n"
                "    cart.m()\n"
                "    x = 3\n"
                "    x.m()\n"
                "    data.m()\n"
                "    self.m()\n"
                "    self.client.m()\n"
            ),
        },
    )
    assert _edges(out) == {
        ("b.py", None, "a.py::A", "import"),
        ("b.py", None, "a.py::A", "call"),
    }


def test_import_inside_function_uses_enclosing_symbol(tmp_path: Path) -> None:
    out = _extract(
        tmp_path,
        {"a.py": "def f():\n    pass\n", "b.py": "def run():\n    from a import f\n    f()\n"},
    )
    assert ("b.py", "b.py::run", "a.py::f", "import") in _edges(out)


def test_star_import_ignored(tmp_path: Path) -> None:
    out = _extract(tmp_path, {"a.py": "def f():\n    pass\n", "b.py": "from a import *\nf()\n"})
    assert out["references"] == []


def test_dedupe_keeps_smallest_line(tmp_path: Path) -> None:
    src = "from a import f\n\ndef run():\n    f()\n    f()\n"
    out = _extract(tmp_path, {"a.py": "def f():\n    pass\n", "b.py": src})
    calls = [r for r in out["references"] if r["kind"] == "call"]
    assert len(calls) == 1
    assert calls[0]["line"] == 4


def test_bad_files_skipped_others_kept(tmp_path: Path) -> None:
    out = _extract(
        tmp_path,
        {
            "good.py": "def f():\n    pass\n",
            "bad.py": "def (:\n",
            "latin.py": b"x = '\xe9\xff'\n",
            "nul.py": b"x = 1\x00\n",
        },
    )
    assert [s["id"] for s in out["symbols"]] == ["good.py::f"]
    assert [s["file"] for s in out["skipped"]] == ["bad.py", "latin.py", "nul.py"]
    assert all(isinstance(s["reason"], str) and s["reason"] for s in out["skipped"])


def test_unknown_ref_raises(sample: SampleRepo) -> None:
    with pytest.raises(ValueError, match="unknown ref: nope"):
        extract_symbols(sample.path, "nope")


def test_non_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a git repository"):
        extract_symbols(tmp_path, "main")


def test_end_line_of_decorated_function_class_and_method(tmp_path: Path) -> None:
    src = (
        "import functools\n"  # 1
        "\n"  # 2
        "@functools.cache\n"  # 3
        "def deco(x):\n"  # 4
        "    y = x\n"  # 5
        "    return y\n"  # 6
        "\n"  # 7
        "class A:\n"  # 8
        "    def m(self):\n"  # 9
        "        return 1\n"  # 10
        "\n"  # 11
        "    def n(self):\n"  # 12
        "        return 2\n"  # 13
    )
    symbols = _extract(tmp_path, {"m.py": src})["symbols"]
    assert [(s["id"], s["line"], s["end_line"]) for s in symbols] == [
        ("m.py::deco", 4, 6),
        ("m.py::A", 8, 13),
        ("m.py::A.m", 9, 10),
        ("m.py::A.n", 12, 13),
    ]


def test_end_line_of_sample_verify_token(sample: SampleRepo) -> None:
    by_id = {s["id"]: s for s in extract_symbols(sample.path, "main")["symbols"]}
    verify = by_id["shared/auth.py::verify_token"]
    assert (verify["line"], verify["end_line"]) == (8, 9)


# --- fuzz -------------------------------------------------------------------------------------

_MODULES = {"shared.auth": "shared/auth.py", "shared": "shared/__init__.py", "a.b.c": "a/b/c.py"}


def _check(path: str, source: bytes) -> None:
    symbols, refs, reason = parse_module(path, source, _MODULES)
    assert reason is None or (isinstance(reason, str) and reason)
    if reason is not None:
        assert symbols == [] and refs == []
    for s in symbols:
        assert set(s) == {"id", "kind", "name", "file", "line", "end_line"}
        assert s["kind"] in {"function", "class", "method"}
        assert isinstance(s["id"], str) and isinstance(s["name"], str)
        assert s["file"] == path and isinstance(s["line"], int)
    for r in refs:
        assert set(r) == {"id", "from_file", "from_symbol", "to_symbol", "kind", "line"}
        assert r["kind"] in {"import", "call"}
        assert r["from_file"] == path
        assert r["from_symbol"] is None or isinstance(r["from_symbol"], str)
        assert isinstance(r["to_symbol"], str) and isinstance(r["line"], int)
        assert isinstance(r["id"], str)


def _mutate(rng: random.Random, text: str) -> bytes:
    data = bytearray(text.encode())
    kind = rng.randrange(5)
    if kind == 0 and data:
        del data[rng.randrange(len(data)) :]
    elif kind == 1 and data:
        for _ in range(rng.randrange(1, 6)):
            data[rng.randrange(len(data))] = rng.randrange(256)
    elif kind == 2:
        data.insert(rng.randrange(len(data) + 1), rng.choice(b"()[]{}:,.\n\t \"'\\@*"))
    elif kind == 3 and data:
        del data[rng.randrange(len(data))]
    else:
        lines = text.splitlines(keepends=True)
        if lines:
            i = rng.randrange(len(lines))
            lines.insert(i, lines[i])
        return "".join(lines).encode()
    return bytes(data)


def test_fuzz_parse_module_never_raises(sample: SampleRepo) -> None:
    rng = random.Random(1234)
    cases: list[bytes] = []
    cases += [rng.randbytes(rng.randrange(0, 200)) for _ in range(150)]
    alphabet = string.printable
    cases += [
        "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 200))).encode()
        for _ in range(150)
    ]
    texts = [p.read_text() for p in sorted(sample.path.rglob("*.py"))]
    assert len(texts) >= 8
    cases += [_mutate(rng, rng.choice(texts)) for _ in range(250)]
    cases += [
        b"(" * 10000,
        b"[" * 100000,
        b"x = " + b"-" * 100000 + b"1\n",
        b"x = " + b"1+" * 20000 + b"1\n",
        b"x = 1\n" * 100_000,
        b"\xef\xbb\xbfdef f():\n    pass\n",
        b"\x00",
        b"\xed\xa0\x80 = 1",
        b"\xff\xfe\x00",
        b"from ...... import x\n",
        b"from . import *\n",
        b"import a.b.c\n",
        b"import a.b.c as z\nz.f()\n",
        b"if 1:\n\tx = 1\n        y = 2\n",
        b"def f():\n\tif 1:\n        pass\n",
        b"f = lambda: g()\nh = lambda: shared.auth.f()\n",
        b"from shared import auth\nauth.verify_token()\n",
        b"",
    ]
    assert len(cases) >= 500
    for i, data in enumerate(cases):
        _check(f"fuzz/{i}.py", data)


def test_parse_module_bom_and_lambda() -> None:
    symbols, _, reason = parse_module("a.py", b"\xef\xbb\xbfdef f():\n    pass\n", {})
    assert reason is None and [s["id"] for s in symbols] == ["a.py::f"]
