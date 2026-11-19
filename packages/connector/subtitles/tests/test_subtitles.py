import pytest
from pathlib import Path
from cognee_community_connector_subtitles.subtitles import (
    SUBTITLES_SOURCE_NAME,
    SUBTITLES_TABLE_NAME,
    merge_cues_into_dialogue,
    parse_srt,
    parse_vtt,
    process_subtitle_file,
    subtitles_source,
)

try:
    import dlt
    HAS_DLT = True
except ImportError:
    HAS_DLT = False

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parse_srt():
    srt_path = FIXTURES_DIR / "sample.srt"
    content = srt_path.read_text(encoding="utf-8")
    cues = parse_srt(content)

    assert len(cues) == 4
    assert cues[0].speaker == "Alice"
    assert "Welcome everyone" in cues[0].text
    assert cues[0].start_seconds == 1.2
    assert cues[0].end_seconds == 3.45

    assert cues[2].speaker == "Bob"
    assert "benchmarking results" in cues[2].text


def test_parse_vtt():
    vtt_path = FIXTURES_DIR / "sample.vtt"
    content = vtt_path.read_text(encoding="utf-8")
    cues = parse_vtt(content)

    assert len(cues) == 3
    assert cues[0].speaker == "Carol"
    assert "checkout conversion funnel" in cues[0].text
    assert cues[0].start_seconds == 2.1
    assert cues[0].end_seconds == 4.9

    assert cues[2].speaker == "David"
    assert "Awesome work Carol" in cues[2].text


def test_merge_cues_into_dialogue():
    srt_path = FIXTURES_DIR / "sample.srt"
    content = srt_path.read_text(encoding="utf-8")
    cues = parse_srt(content)

    dialogue, speakers, duration = merge_cues_into_dialogue(cues, max_gap_seconds=2.0)

    # Cues 1 & 2 from Alice should merge into 1 paragraph
    # Cues 3 & 4 from Bob should merge into 1 paragraph
    paragraphs = dialogue.split("\n\n")
    assert len(paragraphs) == 2
    assert "**Alice**:" in paragraphs[0]
    assert "Welcome everyone" in paragraphs[0]
    assert "new connector roadmap" in paragraphs[0]

    assert "**Bob**:" in paragraphs[1]
    assert "benchmarking results" in paragraphs[1]
    assert "Latency dropped by 42%" in paragraphs[1]

    assert speakers == ["Alice", "Bob"]
    assert duration == 12.9


def test_process_subtitle_file():
    srt_path = FIXTURES_DIR / "sample.srt"
    doc = process_subtitle_file(srt_path)

    assert doc is not None
    assert doc.title == "Sample"
    assert doc.file_format == "srt"
    assert doc.cue_count == 4
    assert len(doc.speakers) == 2
    assert "# Transcript: Sample" in doc.markdown_content


@pytest.mark.skipif(not HAS_DLT, reason="dlt not installed in test environment")
def test_subtitles_source():
    src = subtitles_source(FIXTURES_DIR)
    assert src.name == SUBTITLES_SOURCE_NAME

    resources = list(src.resources.values())
    assert len(resources) == 1
    res = resources[0]
    assert res.name == SUBTITLES_TABLE_NAME

    # Materialize records
    records = list(res())
    assert len(records) == 2  # sample.srt and sample.vtt

    formats = {r["format"] for r in records}
    assert formats == {"srt", "vtt"}

    for rec in records:
        assert rec["id"].startswith("subtitles:")
        assert "text" in rec
        assert len(rec["text"]) > 50
        assert "duration_seconds" in rec


def test_utf8_bom_and_crlf():
    raw = "\ufeff1\r\n00:00:01,000 --> 00:00:04,000\r\nSpeaker: Line with BOM and CRLF\r\n"
    cues = parse_srt(raw)
    assert len(cues) == 1
    assert cues[0].speaker == "Speaker"
    assert cues[0].text == "Line with BOM and CRLF"
