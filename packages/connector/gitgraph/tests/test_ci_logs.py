"""CI failure rows read from JUnit XML files."""

import random
import string
from pathlib import Path
from typing import Any

import pytest

from cognee_community_connector_gitgraph import ci_logs
from cognee_community_connector_gitgraph.ci_logs import parse_ci_logs, parse_junit

FIXTURES = Path(__file__).parent / "fixtures" / "ci"
SHA = "3f2a9c1d5e7b8a60412c9d3e4f5a6b7c8d9e0f1a"

BILLING_ROW = {
    "id": "billing-tests:billing.test_invoice::test_charge_with_max_age",
    "job": "billing-tests",
    "test_id": "billing.test_invoice::test_charge_with_max_age",
    "file": None,
    "commit_sha": None,
    "kind": "error",
    "message": "TypeError: charge() got an unexpected keyword argument 'max_age'",
}
ORDERS_ROW = {
    "id": "orders-tests:orders.test_checkout::test_checkout_rejects_bad_token",
    "job": "orders-tests",
    "test_id": "orders.test_checkout::test_checkout_rejects_bad_token",
    "file": "orders/test_checkout.py",
    "commit_sha": SHA,
    "kind": "failure",
    "message": "assert False is True",
}


def _suite(body: str, props: str = "") -> bytes:
    return f"<testsuite name='s'>{props}{body}</testsuite>".encode()


def _sha_prop(name: str, value: str) -> str:
    return f"<properties><property name='{name}' value='{value}'/></properties>"


def _rows(data: bytes) -> list[dict[str, Any]]:
    return parse_junit("job", data)


def test_fixture_dir_exact_rows() -> None:
    out = parse_ci_logs([FIXTURES])
    assert out == {"ci_failures": [BILLING_ROW, ORDERS_ROW], "skipped": []}


def test_individual_files() -> None:
    assert parse_ci_logs([FIXTURES / "orders-tests.xml"])["ci_failures"] == [ORDERS_ROW]
    assert parse_ci_logs([FIXTURES / "billing-tests.xml"])["ci_failures"] == [BILLING_ROW]
    assert parse_ci_logs([FIXTURES / "search-tests.xml"]) == {"ci_failures": [], "skipped": []}
    both = parse_ci_logs([FIXTURES / "orders-tests.xml", FIXTURES / "billing-tests.xml"])
    assert both["ci_failures"] == [BILLING_ROW, ORDERS_ROW]


def test_dir_recursion_sorted_and_only_xml(tmp_path: Path) -> None:
    (tmp_path / "deep" / "er").mkdir(parents=True)
    bad = _suite("<testcase classname='c' name='t'><failure message='m'/></testcase>")
    (tmp_path / "z.xml").write_bytes(bad)
    (tmp_path / "deep" / "er" / "a.xml").write_bytes(bad)
    (tmp_path / "notes.txt").write_text("not xml")
    out = parse_ci_logs([tmp_path])
    assert [r["id"] for r in out["ci_failures"]] == ["a:c::t", "z:c::t"]
    assert out["skipped"] == []


def test_nonexistent_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no such path"):
        parse_ci_logs([tmp_path / "missing.xml"])


def test_accepts_str_paths() -> None:
    assert parse_ci_logs([str(FIXTURES)])["ci_failures"] == [BILLING_ROW, ORDERS_ROW]


# --- skipped rows -------------------------------------------------------------------------------


def _skip_reason(tmp_path: Path, data: bytes) -> str:
    file = tmp_path / "bad.xml"
    file.write_bytes(data)
    out = parse_ci_logs([file])
    assert out["ci_failures"] == []
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["file"] == str(file)
    reason = out["skipped"][0]["reason"]
    assert isinstance(reason, str) and reason
    return str(reason)


def test_billion_laughs_skipped(tmp_path: Path) -> None:
    payload = (
        b'<?xml version="1.0"?>\n<!DOCTYPE lolz [\n'
        b'<!ENTITY lol "lol">\n<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n]>\n'
        b"<testsuite>&lol3;</testsuite>"
    )
    assert "DOCTYPE" in _skip_reason(tmp_path, payload)


def test_external_entity_skipped(tmp_path: Path) -> None:
    payload = b'<!DOCTYPE testsuite SYSTEM "file:///etc/passwd"><testsuite/>'
    assert "DOCTYPE" in _skip_reason(tmp_path, payload)


def test_doctype_check_is_case_insensitive(tmp_path: Path) -> None:
    assert "DOCTYPE" in _skip_reason(tmp_path, b"<!doctype x><testsuite/>")
    assert "DOCTYPE" in _skip_reason(tmp_path, b"<testsuite><!eNtItY x 'y'></testsuite>")


def test_malformed_skipped(tmp_path: Path) -> None:
    assert "malformed" in _skip_reason(tmp_path, b"<testsuite><testcase></testsuite>")


def test_not_junit_skipped(tmp_path: Path) -> None:
    assert "JUnit" in _skip_reason(tmp_path, b"<html><body/></html>")


def test_empty_skipped(tmp_path: Path) -> None:
    assert "empty" in _skip_reason(tmp_path, b"")
    assert "empty" in _skip_reason(tmp_path, b"  \n")


def test_oversize_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_logs, "MAX_BYTES", 20)
    assert "too large" in _skip_reason(tmp_path, b"<testsuite>" + b" " * 50 + b"</testsuite>")


def test_max_bytes_default() -> None:
    assert ci_logs.MAX_BYTES == 10 * 1024 * 1024


def test_utf16_skipped(tmp_path: Path) -> None:
    data = "<!DOCTYPE x><testsuite/>".encode("utf-16")
    assert _skip_reason(tmp_path, data)


def test_bad_file_does_not_stop_good_ones(tmp_path: Path) -> None:
    (tmp_path / "a.xml").write_bytes(b"<nope")
    (tmp_path / "b.xml").write_bytes(
        _suite("<testcase classname='c' name='t'><error message='m'/></testcase>")
    )
    out = parse_ci_logs([tmp_path])
    assert [r["id"] for r in out["ci_failures"]] == ["b:c::t"]
    assert [s["file"] for s in out["skipped"]] == [str(tmp_path / "a.xml")]


# --- row details --------------------------------------------------------------------------------


def _case(extra: str = "<failure message='m'/>", attrs: str = "classname='c' name='t'") -> str:
    return f"<testcase {attrs}>{extra}</testcase>"


def test_commit_sha_valid_and_uppercase_normalised() -> None:
    for name in ["commit", "sha", "git_sha", "GITHUB_SHA", "CI_COMMIT_SHA"]:
        rows = _rows(_suite(_case(), _sha_prop(name, "abc1234")))
        assert rows[0]["commit_sha"] == "abc1234"
    rows = _rows(_suite(_case(), _sha_prop("commit", "ABCDEF1234567890ABCDEF1234567890ABCDEF12")))
    assert rows[0]["commit_sha"] == "abcdef1234567890abcdef1234567890abcdef12"


def test_commit_sha_invalid_or_missing_is_none() -> None:
    for value in ["abc123", "g" * 10, "a" * 41, "", "abc 1234", "main"]:
        assert _rows(_suite(_case(), _sha_prop("commit", value)))[0]["commit_sha"] is None
    assert _rows(_suite(_case(), _sha_prop("other", "abc1234")))[0]["commit_sha"] is None
    assert _rows(_suite(_case()))[0]["commit_sha"] is None


def test_commit_sha_from_root_testsuites_and_suite_override() -> None:
    first = "<testsuite name='a'>" + _case() + "</testsuite>"
    second = "<testsuite name='b'>" + _sha_prop("sha", "bbbbbbb") + _case(attrs="name='u'")
    root = "<testsuites>" + _sha_prop("sha", "aaaaaaa") + first + second + "</testsuite>"
    rows = _rows((root + "</testsuites>").encode())
    assert {r["test_id"]: r["commit_sha"] for r in rows} == {"c::t": "aaaaaaa", "u": "bbbbbbb"}


def test_test_id_without_classname_and_file_attr() -> None:
    rows = _rows(_suite(_case(attrs="name='lonely' file='x/test_y.py'")))
    assert rows[0]["test_id"] == "lonely"
    assert rows[0]["id"] == "job:lonely"
    assert rows[0]["file"] == "x/test_y.py"
    assert _rows(_suite(_case()))[0]["file"] is None


def test_only_failure_and_error_testcases() -> None:
    body = (
        _case("", "classname='c' name='pass'")
        + _case("<skipped message='s'/>", "classname='c' name='skip'")
        + _case("<failure message='f'/>", "classname='c' name='fail'")
        + _case("<error message='e'/>", "classname='c' name='err'")
        + _case("<system-out>x</system-out>", "classname='c' name='out'")
    )
    rows = _rows(_suite(body))
    assert [(r["test_id"], r["kind"], r["message"]) for r in rows] == [
        ("c::err", "error", "e"),
        ("c::fail", "failure", "f"),
    ]


def test_message_first_nonempty_line_stripped() -> None:
    rows = _rows(_suite(_case("<failure message='&#10;  &#10;  first  &#10;second'/>")))
    assert rows[0]["message"] == "first"
    rows = _rows(_suite(_case("<failure>\n\n   from text  \nmore</failure>")))
    assert rows[0]["message"] == "from text"
    assert _rows(_suite(_case("<failure/>")))[0]["message"] == ""
    assert _rows(_suite(_case("<failure message=' ' >  \n </failure>")))[0]["message"] == ""


def test_message_attribute_beats_text() -> None:
    rows = _rows(_suite(_case("<failure message='attr'>text</failure>")))
    assert rows[0]["message"] == "attr"
    rows = _rows(_suite(_case("<failure message=''>text</failure>")))
    assert rows[0]["message"] == "text"


def test_message_truncated_to_500() -> None:
    rows = _rows(_suite(_case(f"<failure message='{'x' * 900}'/>")))
    assert rows[0]["message"] == "x" * 500
    rows = _rows(_suite(_case(f"<error>{'y' * 900}</error>")))
    assert rows[0]["message"] == "y" * 500


def test_nested_suites() -> None:
    inner = "<testsuite name='in'>" + _case(attrs="classname='c' name='deep'") + "</testsuite>"
    mid = "<testsuite name='mid'>" + inner + "</testsuite>"
    root = "<testsuites><testsuite name='out'>" + mid + _case(attrs="name='top'") + "</testsuite>"
    rows = _rows((root + "</testsuites>").encode())
    assert [r["test_id"] for r in rows] == ["c::deep", "top"]


def test_nested_suite_inherits_sha() -> None:
    inner = "<testsuite name='in'>" + _case() + "</testsuite>"
    outer = "<testsuite name='out'>" + _sha_prop("commit", "abc1234") + inner + "</testsuite>"
    assert _rows(outer.encode())[0]["commit_sha"] == "abc1234"


def test_duplicate_ids_keep_first_and_sorted() -> None:
    body = (
        _case("<failure message='first'/>", "classname='c' name='b'")
        + _case("<error message='second'/>", "classname='c' name='b'")
        + _case("<failure message='a'/>", "classname='c' name='a'")
    )
    rows = _rows(_suite(body))
    assert [(r["id"], r["kind"], r["message"]) for r in rows] == [
        ("job:c::a", "failure", "a"),
        ("job:c::b", "failure", "first"),
    ]


def test_parse_junit_raises_value_error_only() -> None:
    for data in [b"", b"<a/>", b"<testsuite", b"<!DOCTYPE a><testsuite/>", b"\x00", b"\xff\xfe"]:
        with pytest.raises(ValueError):
            _rows(data)


# --- fuzz ---------------------------------------------------------------------------------------


def _check(data: bytes) -> None:
    try:
        first = parse_junit("fuzz", data)
    except ValueError as exc:
        assert str(exc)
        with pytest.raises(ValueError):
            parse_junit("fuzz", data)
        return
    assert isinstance(first, list)
    assert first == parse_junit("fuzz", data)
    assert [r["id"] for r in first] == sorted({r["id"] for r in first})
    for r in first:
        assert set(r) == {"id", "job", "test_id", "file", "commit_sha", "kind", "message"}
        assert r["job"] == "fuzz" and r["id"] == f"fuzz:{r['test_id']}"
        assert r["kind"] in {"failure", "error"}
        assert r["file"] is None or isinstance(r["file"], str)
        assert r["commit_sha"] is None or len(r["commit_sha"]) >= 7
        assert isinstance(r["message"], str) and len(r["message"]) <= 500


def _mutate(rng: random.Random, data: bytes) -> bytes:
    out = bytearray(data)
    kind = rng.randrange(4)
    if kind == 0 and out:
        del out[rng.randrange(len(out)) :]
    elif kind == 1 and out:
        for _ in range(rng.randrange(1, 6)):
            out[rng.randrange(len(out))] = rng.randrange(256)
    elif kind == 2:
        out.insert(rng.randrange(len(out) + 1), rng.choice(b"<>&\"'/=![]- \n\x00"))
    elif out:
        del out[rng.randrange(len(out))]
    return bytes(out)


def test_fuzz_parse_junit_never_raises_other() -> None:
    rng = random.Random(1234)
    cases: list[bytes] = []
    cases += [rng.randbytes(rng.randrange(0, 200)) for _ in range(150)]
    cases += [
        "".join(rng.choice(string.printable) for _ in range(rng.randrange(0, 200))).encode()
        for _ in range(150)
    ]
    texts = [p.read_bytes() for p in sorted(FIXTURES.glob("*.xml"))]
    assert len(texts) >= 3
    cases += [_mutate(rng, rng.choice(texts)) for _ in range(250)]
    cases += [
        b"<testsuite>" * 20000,
        b"<testsuite>" * 20000 + b"</testsuite>" * 20000,
        b"<testsuite>"
        + b"<testsuite>" * 5000
        + b"<testcase name='a'><failure/></testcase>"
        + b"</testsuite>" * 5001,
        b"<testsuite>" + b"<testcase name='a'><failure/></testcase>" * 20000 + b"</testsuite>",
        b"\xef\xbb\xbf<testsuite/>",
        b'<?xml version="1.0" encoding="nope"?><testsuite/>',
        b'<?xml version="1.0" encoding="utf-16"?><testsuite/>',
        b"<testsuite>&undefined;</testsuite>",
        b"<testsuite><testcase name='\xc3\x28'><failure/></testcase></testsuite>",
        b"<ns:testsuite xmlns:ns='x'/>",
        b"<testsuite xmlns='urn:x'><testcase name='a'><failure/></testcase></testsuite>",
        b"",
    ]
    assert len(cases) >= 500
    for data in cases:
        _check(data)
