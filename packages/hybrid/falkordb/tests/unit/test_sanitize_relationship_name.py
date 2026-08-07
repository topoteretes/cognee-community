"""Unit tests for relationship-name sanitization.

These tests verify that ``FalkorDBAdapter.sanitize_relationship_name``
produces identifiers that FalkorDB's Cypher parser accepts (ASCII-only,
matching ``[A-Za-z_][A-Za-z0-9_]*``). No FalkorDB connection is required.
"""

import re

from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter

# Cypher's grammar for unquoted identifiers, which is what FalkorDB accepts
# for relationship types in patterns like ``[edge:TYPE]``.
_VALID_CYPHER_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sanitize(name: str) -> str:
    # ``sanitize_relationship_name`` is an instance method that doesn't
    # actually touch ``self``, so we call it unbound with ``None`` to avoid
    # constructing a real adapter (which would require a FalkorDB connection).
    return FalkorDBAdapter.sanitize_relationship_name(None, name)  # type: ignore[arg-type]


def test_ascii_unchanged():
    assert _sanitize("knows") == "knows"
    assert _sanitize("works_for") == "works_for"


def test_hyphens_and_spaces_become_underscores():
    assert _sanitize("works-for") == "works_for"
    assert _sanitize("works for") == "works_for"


def test_consecutive_separators_collapse():
    assert _sanitize("works---for") == "works_for"
    assert _sanitize("a   b   c") == "a_b_c"


def test_portuguese_accents_are_folded():
    # Without the fix, Python's Unicode-aware ``\w`` lets these chars pass
    # through unchanged, so the resulting identifier breaks the Cypher parser.
    assert _sanitize("responsável_por") == "responsavel_por"
    assert _sanitize("é_relacionado") == "e_relacionado"
    assert _sanitize("ação") == "acao"


def test_spanish_and_french_accents_are_folded():
    assert _sanitize("niño") == "nino"
    assert _sanitize("café") == "cafe"
    assert _sanitize("über") == "uber"


def test_non_latin_collapses_to_underscore():
    # Cyrillic / CJK have no ASCII fold, so they collapse to underscores
    # and we still get a valid (if opaque) identifier.
    assert _VALID_CYPHER_IDENT.match(_sanitize("знает"))  # "knows" in Russian
    assert _VALID_CYPHER_IDENT.match(_sanitize("知道"))  # "knows" in Chinese


def test_leading_digit_gets_underscore_prefix():
    assert _sanitize("123foo") == "_123foo"


def test_empty_or_only_separators_returns_default():
    assert _sanitize("") == "RELATIONSHIP"
    assert _sanitize("___") == "RELATIONSHIP"
    assert _sanitize("---") == "RELATIONSHIP"


def test_all_outputs_are_valid_cypher_identifiers():
    samples = [
        "knows",
        "responsável_por",
        "ação",
        "café",
        "niño",
        "знает",
        "知道",
        "works-for",
        "123foo",
        "  spaces  ",
        "MIXED-Case_Name",
    ]
    for name in samples:
        result = _sanitize(name)
        assert _VALID_CYPHER_IDENT.match(result), (
            f"{name!r} → {result!r} is not a valid Cypher identifier"
        )
