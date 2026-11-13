"""Runnable example for cognee subtitles connector."""

from pathlib import Path
from cognee_community_connector_subtitles.subtitles import subtitles_source

def main():
    fixtures_dir = Path(__file__).parent.parent / "tests" / "fixtures"
    print(f"Creating subtitles data-source pointing to {fixtures_dir}...")
    source = subtitles_source(fixtures_dir)
    print(f"Source: {source.name}")

    resource = list(source.resources.values())[0]
    records = list(resource())
    print(f"Successfully loaded {len(records)} subtitle documents:\n")

    for rec in records:
        print(f"📄 Document: {rec['name']} ({rec['format'].upper()})")
        print(f"   ID: {rec['id']}")
        print(f"   Duration: {rec['duration_seconds']}s | Speakers: {rec['speakers']}")
        print("   Preview:")
        preview_lines = rec["text"].split("\n")[:8]
        for line in preview_lines:
            print(f"     {line}")
        print("-" * 60)

if __name__ == "__main__":
    main()
