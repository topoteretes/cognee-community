"""How CMS items, collection schemas and pages become text."""

from cognee_community_connector_webflow.render import (
    card_content,
    field_text,
    html_to_text,
    item_content,
    page_content,
    schema_fingerprint,
)

COLLECTION = {
    "id": "c1",
    "displayName": "Blog Posts",
    "singularName": "Blog Post",
    "slug": "posts",
    "siteName": "Acme",
}
SCHEMA = {
    "fields": [
        {"slug": "name", "displayName": "Name", "type": "PlainText", "isRequired": True},
        {"slug": "slug", "displayName": "Slug", "type": "PlainText", "isRequired": True},
        {"slug": "body", "displayName": "Post Body", "type": "RichText"},
        {
            "slug": "category",
            "displayName": "Category",
            "type": "Option",
            "helpText": "Where it is listed",
            "validations": {
                "options": [{"id": "o1", "name": "News"}, {"id": "o2", "name": "Guides"}]
            },
        },
        {"slug": "featured", "displayName": "Featured", "type": "Switch"},
        {"slug": "author", "displayName": "Author", "type": "Reference"},
        {"slug": "cover", "displayName": "Cover", "type": "Image"},
        {"slug": "views", "displayName": "Views", "type": "Number", "isRequired": True},
    ]
}


# -- rich text ---------------------------------------------------------------
def test_html_keeps_headings_lists_and_link_targets():
    html = (
        "<h2>Setup</h2><p>Read the <a href='https://example.com/docs'>docs</a>.</p>"
        "<ul><li>One</li><li>Two</li></ul>"
    )
    assert html_to_text(html) == (
        "## Setup\n\nRead the docs (https://example.com/docs).\n\n- One\n- Two"
    )


def test_html_drops_scripts_styles_and_anchor_only_links():
    html = "<style>p{}</style><p>Hi <a href='#top'>top</a><script>alert(1)</script></p>"
    assert html_to_text(html) == "Hi top"


def test_html_decodes_entities_and_collapses_whitespace():
    assert html_to_text("<p>Fish &amp;   chips&nbsp;&nbsp;now</p>") == "Fish & chips now"


def test_html_of_nothing_is_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""
    assert html_to_text("<p></p>") == ""


# -- field values ------------------------------------------------------------
def test_option_ids_become_their_names():
    assert field_text(SCHEMA["fields"][3], "o2") == "Guides"


def test_an_unknown_option_id_is_kept_as_is():
    assert field_text(SCHEMA["fields"][3], "gone") == "gone"


def test_switch_reads_yes_or_no():
    switch = SCHEMA["fields"][4]
    assert field_text(switch, True) == "yes"
    assert field_text(switch, False) == "no"


def test_references_and_files_say_nothing_in_text():
    assert field_text({"type": "Reference"}, "abc") == ""
    assert field_text({"type": "MultiReference"}, ["a", "b"]) == ""
    assert field_text({"type": "File"}, {"url": "https://x/y.pdf"}) == ""


def test_images_contribute_their_alt_text_only():
    assert field_text({"type": "Image"}, {"url": "https://x/y.png", "alt": "A red kettle"}) == (
        "A red kettle"
    )
    assert field_text({"type": "Image"}, {"url": "https://x/y.png", "alt": None}) == ""
    gallery = [{"alt": "One"}, {"alt": ""}, {"alt": "Two"}]
    assert field_text({"type": "MultiImage"}, gallery) == "One, Two"


def test_numbers_dates_and_empty_values():
    assert field_text({"type": "Number"}, 0) == "0"
    assert (
        field_text({"type": "DateTime"}, "2026-01-02T00:00:00.000Z") == "2026-01-02T00:00:00.000Z"
    )
    assert field_text({"type": "PlainText"}, "") == ""
    assert field_text({"type": "PlainText"}, None) == ""


# -- documents ---------------------------------------------------------------
def test_item_document_lists_labelled_fields_in_schema_order():
    item = {
        "id": "i1",
        "fieldData": {
            "name": "Launch day",
            "slug": "launch-day",
            "body": "<p>We shipped.</p>",
            "category": "o1",
            "featured": True,
            "author": "ref1",
            "cover": {"url": "https://x/y.png", "alt": ""},
            "views": 0,
        },
    }

    assert item_content(COLLECTION, SCHEMA, item) == (
        'Webflow Blog Post "Launch day" (collection: Blog Posts, site: Acme)\n'
        "Slug: launch-day\n"
        "Post Body: We shipped.\n"
        "Category: News\n"
        "Featured: yes\n"
        "Views: 0"
    )


def test_long_values_sit_under_their_label():
    item = {"id": "i1", "fieldData": {"name": "A", "body": "<p>One</p><p>Two</p>"}}

    content = item_content(COLLECTION, SCHEMA, item)

    assert "Post Body:\nOne\n\nTwo" in content


def test_drafts_say_so_and_unknown_fields_use_their_slug():
    item = {"id": "i1", "isDraft": True, "fieldData": {"name": "A", "legacy-note": "kept"}}

    content = item_content(COLLECTION, SCHEMA, item)

    assert "Status: draft" in content
    assert "legacy-note: kept" in content


def test_an_item_without_a_name_falls_back_to_its_id():
    assert 'Webflow Blog Post "i1"' in item_content(COLLECTION, SCHEMA, {"id": "i1"})


def test_card_describes_fields_options_and_help_text():
    assert card_content(COLLECTION, SCHEMA) == (
        'Webflow collection "Blog Posts" (site: Acme)\n'
        "Slug: posts\n"
        "Each item is a Blog Post.\n"
        "Fields:\n"
        "- Post Body (RichText)\n"
        "- Category (Option: News, Guides): Where it is listed\n"
        "- Featured (Switch)\n"
        "- Author (Reference)\n"
        "- Cover (Image)\n"
        "- Views (Number, required)"
    )


def test_page_document_has_title_path_description_and_text():
    page = {"id": "p1", "title": "About", "slug": "about", "seo": {"description": "Who we are"}}
    nodes = [
        {"type": "text", "text": {"text": "We make kettles.", "html": "<p>We make kettles.</p>"}},
        {"type": "text", "text": {"text": None, "html": "<h2>Team</h2>"}},
        {"type": "image", "image": {"alt": "The team", "assetId": "a"}},
        {"type": "image", "image": {"alt": None, "assetId": "b"}},
        {"type": "select", "choices": [{"value": "x", "text": "X"}]},
        {
            "type": "component-instance",
            "propertyOverrides": [{"text": {"text": "Call us", "html": None}}],
        },
    ]

    assert page_content("Acme", page, nodes) == (
        'Webflow page "About" (site: Acme)\n'
        "Path: /about\n"
        "Description: Who we are\n"
        "\n"
        "We make kettles.\n"
        "## Team\n"
        "The team\n"
        "Call us"
    )


# -- schema fingerprint ------------------------------------------------------
def test_fingerprint_changes_with_the_schema_not_with_unrelated_keys():
    base = schema_fingerprint(SCHEMA)
    assert schema_fingerprint({**SCHEMA, "fieldGroups": []}) == base

    renamed = {"fields": [dict(f) for f in SCHEMA["fields"]]}
    renamed["fields"][3]["validations"] = {"options": [{"id": "o1", "name": "Press"}]}
    assert schema_fingerprint(renamed) != base
