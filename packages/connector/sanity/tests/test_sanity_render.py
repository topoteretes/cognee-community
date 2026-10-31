"""How a Sanity document is turned into the text cognee cognifies."""

from fake_sanity import block

from cognee_community_connector_sanity.sanity import (
    _document_row,
    _references,
    _render,
    _Scope,
)

SCOPE = _Scope(project_id="p", dataset="production", document_types=("article",))
LABELS = {
    "author-1": {"rev": "r", "type": "author", "label": "Alice"},
    "tag-1": {"rev": "r", "type": "tag", "label": "Billing"},
    "page-2": {"rev": "r", "type": "page", "label": "Refund policy"},
}


def test_portable_text_becomes_paragraphs_with_list_items_and_links():
    body = [
        block("Intro paragraph.", "a"),
        block("First step", "b", listItem="bullet", level=1),
        block("Nested step", "c", listItem="bullet", level=2),
        block(
            "See the docs",
            "d",
            marks=["m1"],
            markDefs=[{"_key": "m1", "_type": "link", "href": "https://example.com"}],
        ),
        block(
            "Read the refund policy",
            "e",
            marks=["m2"],
            markDefs=[
                {
                    "_key": "m2",
                    "_type": "internalLink",
                    "reference": {"_type": "reference", "_ref": "page-2"},
                }
            ],
        ),
    ]
    text = _render(body, LABELS)
    assert text.splitlines() == [
        "Intro paragraph.",
        "- First step",
        "  - Nested step",
        "See the docs (https://example.com)",
        "Read the refund policy (Refund policy)",
    ]


def test_fields_slugs_references_images_and_nested_objects():
    doc = {
        "_id": "a1",
        "_type": "article",
        "_rev": "x",
        "_updatedAt": "t",
        "title": "Billing FAQ",
        "slug": {"_type": "slug", "current": "billing-faq"},
        "author": {"_type": "reference", "_ref": "author-1"},
        "tags": [{"_type": "reference", "_ref": "tag-1", "_key": "k"}],
        "publishedAt": "2026-10-01",
        "featured": True,
        "readingMinutes": 4,
        "mainImage": {
            "_type": "image",
            "asset": {"_ref": "image-abc-200x200-png"},
            "alt": "A chart",
        },
        "seo": {"_type": "seo", "metaTitle": "Billing questions"},
        "body": [block("Invoices are sent monthly.")],
    }
    row = _document_row(SCOPE, doc, LABELS)
    assert row["title"] == "Billing FAQ"
    lines = row["content"].splitlines()
    assert lines[0] == "Sanity article in production"
    for expected in (
        "Slug: billing-faq",
        "Author: Alice",
        "Tags: Billing",
        "Published at: 2026-10-01",
        "Featured: yes",
        "Reading minutes: 4",
        "Main image: A chart",
        "Seo: Meta title: Billing questions",
    ):
        assert expected in lines, expected
    assert "Body: Invoices are sent monthly." in lines
    assert "image-abc" not in row["content"], "asset ids are not content"
    assert "Billing FAQ" not in row["content"], "the title is not repeated (cognee adds it)"


def test_a_document_without_a_title_field_is_named_by_type_and_id():
    row = _document_row(SCOPE, {"_id": "x9", "_type": "article", "count": 2}, {})
    assert row["title"] == "article x9"


def test_references_are_collected_from_fields_and_annotations_but_not_assets():
    doc = {
        "author": {"_ref": "author-1"},
        "tags": [{"_ref": "tag-1"}, {"_ref": "drafts.tag-2"}],
        "image": {"asset": {"_ref": "image-abc-1x1-png"}},
        "body": [block("x", markDefs=[{"_key": "m", "reference": {"_ref": "page-2"}}])],
    }
    assert _references(doc) == {"author-1", "tag-1", "tag-2", "page-2"}
