"""How Gramps objects become documents: dates, names, handles resolved, privacy, tags."""

import pytest
from fake_gramps import BASE_URL, FakeGramps, date, small_tree
from helpers import by_id, client_for, family, person, place, run

from cognee_community_connector_gramps.gramps import NODE_SET_COLUMN, format_date, format_name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(1819, 5, 24), "24 May 1819"),
        (date(1819), "1819"),
        (date(1819, 8), "Aug 1819"),
        (date(1820, modifier=3), "about 1820"),
        (date(1820, modifier=1), "before 1820"),
        (date(1820, modifier=2), "after 1820"),
        (date(1820, quality=1), "estimated 1820"),
        (
            {"dateval": [0, 0, 1800, False, 0, 0, 1810, False], "modifier": 4, "quality": 0},
            "between 1800 and 1810",
        ),
        (
            {"dateval": [0, 0, 1800, False, 0, 0, 1810, False], "modifier": 5, "quality": 0},
            "from 1800 to 1810",
        ),
        ({"dateval": [0, 0, 0, False], "modifier": 6, "text": "in the war"}, "in the war"),
        ({"dateval": [0, 0, 0, False], "modifier": 0, "text": ""}, ""),
        (None, ""),
    ],
)
def test_dates_are_written_out(value, expected):
    assert format_date(value) == expected


def test_names_join_title_given_prefixed_surnames_and_suffix():
    name = {
        "title": "Dr.",
        "first_name": "Jan",
        "surname_list": [{"prefix": "van", "surname": "Dijk"}, {"surname": "Smit"}],
        "suffix": "Jr.",
    }
    assert format_name(name) == "Dr. Jan van Dijk Smit, Jr."
    assert format_name({"surname_list": []}) == ""


def _rows(fake, **kwargs):
    rows, _ = run(client_for(fake), {}, **kwargs)
    return by_id(rows)


def test_a_person_document_resolves_relatives_events_places_and_notes_by_name():
    rows = _rows(small_tree())
    mary = rows[person("p_mary")]

    assert mary["title"] == "Mary Smith"
    assert mary["url"] == f"{BASE_URL}/person/I0003"
    text = mary["content"]
    assert "Gender: female" in text
    assert "Born: 21 Nov 1840, London, England" in text
    assert "Died: 22 Jan 1901" in text
    assert "Parents: John Smith (born 1819) (father); Anna Jones (born 1819) (mother)" in text
    assert "- Occupation: Schoolteacher" in text
    assert "Kept a diary all her life." in text
    assert "Tags: Family" in text
    # no raw handle leaks into the text
    for handle in ("p_john", "p_anna", "pl_lon", "e_cb", "n1", "f1", "t1"):
        assert handle not in text
    assert mary[NODE_SET_COLUMN] == ["gramps:people", "gramps:tag:Family"]

    john = rows[person("p_john")]["content"]
    assert "Partner: Anna Jones (born 1819) (married)" in john
    assert "Children with Anna Jones (born 1819): Mary Smith (born 1840)" in john


def test_a_family_document_names_the_couple_children_and_events():
    fam = _rows(small_tree())[family("f1")]

    assert fam["title"] == "Family of John Smith (born 1819) and Anna Jones (born 1819)"
    assert "Relationship: Married" in fam["content"]
    assert "- Mary Smith (born 1840)" in fam["content"]
    assert "- Marriage: 10 Feb 1840, London, England" in fam["content"]
    assert fam[NODE_SET_COLUMN] == ["gramps:families"]


def test_places_are_documents_only_when_included():
    assert not any(row_id.startswith("gramps:place:") for row_id in _rows(small_tree()))

    rows = _rows(small_tree(), include=("places",))
    assert set(rows) == {place("pl_eng"), place("pl_lon")}
    assert "Part of: England" in rows[place("pl_lon")]["content"]


def test_private_records_and_references_stay_out_unless_asked_for():
    fake = small_tree()
    fake.objects["people"]["p_anna"]["private"] = True
    fake.objects["notes"]["n1"]["private"] = True
    fake.objects["people"]["p_mary"]["event_ref_list"][2]["private"] = True  # the occupation

    rows = _rows(fake)
    assert person("p_anna") not in rows
    mary = rows[person("p_mary")]["content"]
    assert "Anna" not in mary
    assert "diary" not in mary
    assert "Schoolteacher" not in mary
    assert "Anna" not in rows[family("f1")]["content"]

    rows = _rows(fake, include_private=True)
    assert person("p_anna") in rows
    assert "diary" in rows[person("p_mary")]["content"]


def test_internal_gedcom_fields_are_left_out():
    fake = small_tree()
    fake.objects["people"]["p_john"]["attribute_list"] = [
        {"type": "_UID", "value": "ABC123", "private": False},
        {"type": "Nickname", "value": "Jack", "private": False},
    ]
    john = _rows(fake)[person("p_john")]["content"]
    assert "ABC123" not in john
    assert "- Nickname: Jack" in john


def test_an_unnamed_person_still_gets_a_title():
    fake = FakeGramps()
    fake.add_person("p1", "", "")
    assert _rows(fake)[person("p1")]["title"] == "Unnamed person I0001"


def test_a_looping_place_hierarchy_does_not_hang():
    fake = FakeGramps()
    fake.add_place("a", "Alpha", parent="b")
    fake.add_place("b", "Beta", parent="a")
    rows = _rows(fake, include=("places",))
    assert "Full name: Alpha, Beta" in rows[place("a")]["content"]
