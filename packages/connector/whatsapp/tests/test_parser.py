"""Parser tests on synthetic exports.

Every fixture here is made up: the names, numbers and messages are invented,
but the line shapes, invisible characters and placeholders match what Android
and iPhone write into real exports.
"""

import zipfile
from datetime import date, datetime

import pytest

from cognee_community_connector_whatsapp.parser import (
    ExportFormatError,
    parse_export,
    read_export,
)

ANDROID = (
    "14/10/26, 9:15\u202fam - Messages and calls are end-to-end encrypted. "
    "Only people in this chat can read, listen to, or share them.\n"
    "14/10/26, 9:16\u202fam - Asha: Supplier quoted 420 per kg\n"
    "14/10/26, 9:17\u202fam - Ravi: Can they deliver by Friday?\n"
    "Also ask about the GST invoice\n"
    "14/10/26, 1:05\u202fpm - Asha: <Media omitted>\n"
    "14/10/26, 1:06\u202fpm - Ravi: This message was deleted\n"
    "15/10/26, 12:30\u202fam - Asha: Confirmed for Friday <This message was edited>\n"
    "15/10/26, 8:00\u202fam - Asha added Meera\n"
)

IPHONE = (
    "[10/14/26, 9:15:42\u202fAM] Family: \u200eMessages and calls are end-to-end encrypted.\n"
    "[10/14/26, 9:16:03\u202fAM] Asha: Dinner at 8?\n"
    "\u200e[10/14/26, 9:17:10\u202fAM] Ravi: \u200eimage omitted\n"
    "\u200e[10/14/26, 9:18:00\u202fAM] Ravi: \u200e<attached: 00000012-PHOTO-2026-10-14.jpg>\n"
    "[10/14/26, 12:01:00\u202fPM] \u202a+91 98765 43210\u202c: Running late\n"
)


def test_android_lines_continuations_and_markers():
    export = parse_export(ANDROID, chat_name="Suppliers")

    assert export.date_order == "dmy"
    assert not export.ambiguous_date_order
    texts = [(m.sender, m.text) for m in export.messages]
    assert texts[0][0] is None  # encryption notice is a system event
    assert ("Asha", "Supplier quoted 420 per kg") in texts
    # The second line has no timestamp, so it belongs to Ravi's message.
    assert ("Ravi", "Can they deliver by Friday?\nAlso ask about the GST invoice") in texts
    assert ("Asha", "[media]") in texts
    # Deleted messages are dropped and the edited marker is stripped.
    assert all("deleted" not in text for _, text in texts)
    assert ("Asha", "Confirmed for Friday") in texts
    assert (None, "Asha added Meera") in texts


def test_android_12_hour_clock_and_midnight():
    export = parse_export(ANDROID, chat_name="Suppliers")
    stamps = {m.text: m.sent_at for m in export.messages}
    assert stamps["[media]"] == datetime(2026, 10, 14, 13, 5)
    assert stamps["Confirmed for Friday"] == datetime(2026, 10, 15, 0, 30)


def test_iphone_format_month_first_and_invisible_marks():
    export = parse_export(IPHONE, chat_name="Family")

    assert export.date_order == "mdy"
    by_text = {m.text: m for m in export.messages}
    assert by_text["Dinner at 8?"].sent_at == datetime(2026, 10, 14, 9, 16, 3)
    assert by_text["[image]"].sender == "Ravi"
    assert "[attachment: 00000012-PHOTO-2026-10-14.jpg]" in by_text
    # The direction marks around the number are removed.
    assert by_text["Running late"].sender == "+91 98765 43210"


def test_ambiguous_dates_fall_back_to_day_first_and_say_so():
    text = "01/02/26, 10:00 - Asha: hello\n03/04/26, 11:00 - Ravi: hi\n"
    export = parse_export(text, chat_name="x")
    assert export.date_order == "dmy"
    assert export.ambiguous_date_order
    assert export.messages[0].sent_at.date() == date(2026, 2, 1)


def test_date_order_override():
    text = "01/02/26, 10:00 - Asha: hello\n"
    export = parse_export(text, chat_name="x", date_order="mdy")
    assert export.messages[0].sent_at.date() == date(2026, 1, 2)
    assert not export.ambiguous_date_order


def test_24_hour_dotted_times_and_four_digit_years():
    text = "2026-10-14 21.15 - Asha: late night\n"
    export = parse_export(text, chat_name="x")
    assert export.date_order == "ymd"
    assert export.messages[0].sent_at == datetime(2026, 10, 14, 21, 15)


def test_colon_inside_quoted_system_text_is_not_a_sender():
    text = '14/10/26, 10:00 - Asha changed the subject to "Trip: Goa"\n'
    message = parse_export(text, chat_name="x").messages[0]
    assert message.sender is None


def test_lines_before_the_first_timestamp_are_counted_not_kept():
    text = "garbage header\n14/10/26, 10:00 - Asha: hi\n"
    export = parse_export(text, chat_name="x")
    assert export.unparsed_lines == 1
    assert [m.text for m in export.messages] == ["hi"]


def test_days_groups_messages_by_calendar_day():
    export = parse_export(ANDROID, chat_name="Suppliers")
    days = export.days()
    assert sorted(days) == [date(2026, 10, 14), date(2026, 10, 15)]


def test_not_an_export_raises():
    with pytest.raises(ExportFormatError):
        parse_export("just some notes\nwith no timestamps\n", chat_name="x")


def test_read_android_txt_takes_chat_name_from_file(tmp_path):
    path = tmp_path / "WhatsApp Chat with Suppliers.txt"
    path.write_text("\ufeff" + ANDROID, encoding="utf-8")
    export = read_export(path)
    assert export.chat_name == "Suppliers"
    assert export.messages


def test_read_iphone_zip_finds_chat_txt(tmp_path):
    path = tmp_path / "WhatsApp Chat - Family.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("_chat.txt", IPHONE)
        archive.writestr("00000012-PHOTO-2026-10-14.jpg", b"not really a photo")
    export = read_export(path)
    assert export.chat_name == "Family"
    assert len(export.messages) == 5


def test_zip_without_chat_text_raises(tmp_path):
    path = tmp_path / "broken.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("photo.jpg", b"x")
    with pytest.raises(ExportFormatError):
        read_export(path)
