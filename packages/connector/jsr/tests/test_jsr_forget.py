"""Tests for forget-on-delete and tombstone emission."""

from fake_jsr import FakeJSRClient

from cognee_community_connector_jsr.jsr import _iter_jsr_rows


def test_tombstone_emitted_when_package_deleted():
    fake = FakeJSRClient()
    state = {}

    # Run 1: two packages
    rows1 = list(
        _iter_jsr_rows(
            client=fake,
            scopes=["std"],
            package_tuples=[],
            include_docs=True,
            previous_state=state,
        )
    )
    assert len(rows1) == 2
    assert "jsr:@std/fs" in state
    assert "jsr:@std/path" in state

    # Delete 'fs' package upstream
    del fake.scopes["std"]["fs"]

    # Run 2: 'fs' should be emitted as a tombstone
    rows2 = list(
        _iter_jsr_rows(
            client=fake,
            scopes=["std"],
            package_tuples=[],
            include_docs=True,
            previous_state=state,
        )
    )
    assert len(rows2) == 1
    tombstone = rows2[0]
    assert tombstone["id"] == "jsr:@std/fs"
    assert tombstone["_deleted"] is True
    assert "jsr:@std/fs" not in state


def test_tombstone_emitted_when_scope_deselected():
    fake = FakeJSRClient()
    state = {}

    # Run 1: scope std
    list(
        _iter_jsr_rows(
            client=fake,
            scopes=["std"],
            package_tuples=[],
            include_docs=True,
            previous_state=state,
        )
    )

    # Run 2: only target path explicitly (fs is deselected)
    rows2 = list(
        _iter_jsr_rows(
            client=fake,
            scopes=[],
            package_tuples=[("std", "path")],
            include_docs=True,
            previous_state=state,
        )
    )
    assert len(rows2) == 1
    assert rows2[0]["id"] == "jsr:@std/fs"
    assert rows2[0]["_deleted"] is True
