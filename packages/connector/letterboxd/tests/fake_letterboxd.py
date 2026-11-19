"""A realistic fake Letterboxd export for tests: real ZIP files, no network.

The CSV shapes mirror a real export downloaded from letterboxd.com
(Settings -> Import & Export -> Request Export):

  - watched.csv  : Name, Year, Letterboxd URI (+ Date in newer exports)
  - ratings.csv  : Name, Year, Rating, Letterboxd URI   (10-point scale)
  - reviews.csv  : Name, Year, Letterboxd URI, Rating, Review, Date
  - diary.csv    : Date, Name, Year, Letterboxd URI, Rating, Tags,
                   Watched Date, Release Date, Rewatch, Review
  - lists/*.csv  : Position, Name, Year, Letterboxd URI (+ Details in some)
  - watchlist.csv: same shape as watched.csv (missing in older exports)

The default fixture covers the interesting cases: a film with a rating but no
review, a film with a full review, a film watched twice (one rewatch with
tags), a film in two lists, a film only in watched.csv, and a 3-film list.
"""

import csv
import io
import zipfile
from pathlib import Path

from cognee_community_connector_letterboxd.letterboxd import (
    ExportData,
    LetterboxdList,
    _read_csv,
)

PARASITE = "https://letterboxd.com/film/parasite/"
GET_OUT = "https://letterboxd.com/film/get-out/"
MAD_MAX = "https://letterboxd.com/film/mad-max-fury-road/"
HEAT = "https://letterboxd.com/film/heat/"
WHIPLASH = "https://letterboxd.com/film/whiplash/"
MOONLIGHT = "https://letterboxd.com/film/moonlight/"

GET_OUT_REVIEW = (
    "A masterclass in tension. Every scene peels back another layer, and the\n"
    "third act lands like a trapdoor. I have not held my breath like that in years."
)
EDITED_GET_OUT_REVIEW = (
    "Rewatched with my sister and liked it even more. The sunken-place sequence\n"
    "is still the scariest thing I have seen this decade."
)

WATCHED_COLUMNS = ["Name", "Year", "Letterboxd URI", "Date"]
RATINGS_COLUMNS = ["Name", "Year", "Rating", "Letterboxd URI"]
REVIEWS_COLUMNS = ["Name", "Year", "Letterboxd URI", "Rating", "Review", "Date"]
DIARY_COLUMNS = [
    "Date",
    "Name",
    "Year",
    "Letterboxd URI",
    "Rating",
    "Tags",
    "Watched Date",
    "Release Date",
    "Rewatch",
    "Review",
]
LIST_COLUMNS = ["Position", "Name", "Year", "Letterboxd URI"]
LIST_WITH_DETAILS_COLUMNS = ["Position", "Name", "Year", "Letterboxd URI", "Details"]


def default_export() -> dict[str, tuple[list[str], list[dict]]]:
    """The fixture export as ``<archive path> -> (columns, rows)``."""
    return {
        "watched.csv": (
            WATCHED_COLUMNS,
            [
                {
                    "Name": "Parasite",
                    "Year": "2019",
                    "Letterboxd URI": PARASITE,
                    "Date": "2024-01-05",
                },
                {
                    "Name": "Get Out",
                    "Year": "2017",
                    "Letterboxd URI": GET_OUT,
                    "Date": "2024-02-10",
                },
                {
                    "Name": "Mad Max: Fury Road",
                    "Year": "2015",
                    "Letterboxd URI": MAD_MAX,
                    "Date": "2024-06-10",
                },
                {
                    "Name": "Heat",
                    "Year": "1995",
                    "Letterboxd URI": HEAT,
                    "Date": "2024-03-01",
                },
                {
                    "Name": "Whiplash",
                    "Year": "2014",
                    "Letterboxd URI": WHIPLASH,
                    "Date": "2024-04-12",
                },
            ],
        ),
        "ratings.csv": (
            RATINGS_COLUMNS,
            [
                {
                    "Name": "Parasite",
                    "Year": "2019",
                    "Rating": "7.5",
                    "Letterboxd URI": PARASITE,
                },
                {
                    "Name": "Get Out",
                    "Year": "2017",
                    "Rating": "8",
                    "Letterboxd URI": GET_OUT,
                },
            ],
        ),
        "reviews.csv": (
            REVIEWS_COLUMNS,
            [
                {
                    "Name": "Get Out",
                    "Year": "2017",
                    "Letterboxd URI": GET_OUT,
                    "Rating": "8",
                    "Review": GET_OUT_REVIEW,
                    "Date": "2024-02-10",
                },
            ],
        ),
        "diary.csv": (
            DIARY_COLUMNS,
            [
                {
                    "Date": "2024-01-05",
                    "Name": "Parasite",
                    "Year": "2019",
                    "Letterboxd URI": PARASITE,
                    "Rating": "7.5",
                    "Tags": "",
                    "Watched Date": "2024-01-05",
                    "Release Date": "2019-05-30",
                    "Rewatch": "",
                    "Review": "",
                },
                {
                    "Date": "2024-02-10",
                    "Name": "Get Out",
                    "Year": "2017",
                    "Letterboxd URI": GET_OUT,
                    "Rating": "8",
                    "Tags": "",
                    "Watched Date": "2024-02-10",
                    "Release Date": "2017-02-24",
                    "Rewatch": "",
                    "Review": "",
                },
                {
                    "Date": "2024-05-01",
                    "Name": "Mad Max: Fury Road",
                    "Year": "2015",
                    "Letterboxd URI": MAD_MAX,
                    "Rating": "",
                    "Tags": "",
                    "Watched Date": "2024-05-01",
                    "Release Date": "2015-05-13",
                    "Rewatch": "",
                    "Review": "",
                },
                {
                    "Date": "2024-06-10",
                    "Name": "Mad Max: Fury Road",
                    "Year": "2015",
                    "Letterboxd URI": MAD_MAX,
                    "Rating": "8",
                    "Tags": "action, rewatch",
                    "Watched Date": "2024-06-10",
                    "Release Date": "2015-05-13",
                    "Rewatch": "Yes",
                    "Review": "Even better the second time.",
                },
            ],
        ),
        "lists/Favourites.csv": (
            LIST_COLUMNS,
            [
                {"Position": "1", "Name": "Parasite", "Year": "2019", "Letterboxd URI": PARASITE},
                {
                    "Position": "2",
                    "Name": "Mad Max: Fury Road",
                    "Year": "2015",
                    "Letterboxd URI": MAD_MAX,
                },
                {"Position": "3", "Name": "Whiplash", "Year": "2014", "Letterboxd URI": WHIPLASH},
            ],
        ),
        # Some exports add a Details column; unknown columns must be ignored.
        "lists/Korean Cinema.csv": (
            LIST_WITH_DETAILS_COLUMNS,
            [
                {
                    "Position": "1",
                    "Name": "Parasite",
                    "Year": "2019",
                    "Letterboxd URI": PARASITE,
                    "Details": "Watched with subtitles",
                },
            ],
        ),
    }


def csv_text(columns: list[str], rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return out.getvalue()


def write_zip(path: Path, files: dict[str, tuple[list[str], list[dict]]] | None = None) -> Path:
    """Write the fixture (or an override) as a real export ZIP."""
    files = default_export() if files is None else files
    with zipfile.ZipFile(path, "w") as archive:
        for name, (columns, rows) in files.items():
            archive.writestr(name, csv_text(columns, rows))
    return path


def write_dir(path: Path, files: dict[str, tuple[list[str], list[dict]]] | None = None) -> Path:
    """Write the fixture (or an override) as an extracted export folder."""
    files = default_export() if files is None else files
    for name, (columns, rows) in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(csv_text(columns, rows), encoding="utf-8")
    return path


def to_export_data(files: dict[str, tuple[list[str], list[dict]]]) -> ExportData:
    """The same fixture, already parsed, for service-injection tests."""
    data = ExportData()
    for name, (columns, rows) in files.items():
        parsed = _read_csv(csv_text(columns, rows))
        if name == "watched.csv":
            data.watched = parsed
        elif name == "ratings.csv":
            data.ratings = parsed
        elif name == "reviews.csv":
            data.reviews = parsed
        elif name == "diary.csv":
            data.diary = parsed
        elif name == "watchlist.csv":
            data.watchlist = parsed
        elif name.startswith("lists/"):
            data.lists.append(LetterboxdList(name=name.split("/", 1)[1][:-4], rows=parsed))
    return data


# ------------------------------------------------------------- export variants
def _without(files: dict, *names: str) -> dict:
    return {name: value for name, value in files.items() if name not in names}


def with_edited_review(files: dict) -> dict:
    """A newer export whose Get Out review was rewritten."""
    out = dict(files)
    reviews_columns, review_rows = out["reviews.csv"]
    out["reviews.csv"] = (
        reviews_columns,
        [
            {**row, "Review": EDITED_GET_OUT_REVIEW} if row["Letterboxd URI"] == GET_OUT else row
            for row in review_rows
        ],
    )
    return out


def without_film(files: dict, uri: str) -> dict:
    """A newer export from which one film vanished entirely."""
    out = {}
    for name, (columns, rows) in files.items():
        kept = [row for row in rows if row.get("Letterboxd URI") != uri]
        out[name] = (columns, kept)
    return out


def without_list(files: dict, list_path: str) -> dict:
    """A newer export from which one list was deleted."""
    return _without(files, list_path)


def with_extra_columns(files: dict) -> dict:
    """A future export that added an unknown column to watched.csv."""
    out = dict(files)
    columns, rows = out["watched.csv"]
    out["watched.csv"] = (
        [*columns, "Description"],
        [{**row, "Description": "ignore me"} for row in rows],
    )
    return out


def without_tags_column(files: dict) -> dict:
    """An older export whose diary.csv has no Tags column."""
    out = dict(files)
    columns, rows = out["diary.csv"]
    out["diary.csv"] = (
        [column for column in columns if column != "Tags"],
        [{k: v for k, v in row.items() if k != "Tags"} for row in rows],
    )
    return out


def with_watchlist(files: dict) -> dict:
    """An export with a watchlist.csv (Moonlight is not watched yet)."""
    out = dict(files)
    out["watchlist.csv"] = (
        WATCHED_COLUMNS,
        [{"Name": "Moonlight", "Year": "2016", "Letterboxd URI": MOONLIGHT, "Date": ""}],
    )
    return out


def without_watched(files: dict) -> dict:
    """A broken export: watched.csv, the one required file, is missing."""
    return _without(files, "watched.csv")


class FakeLetterboxdService:
    """Service injection without a filesystem: returns a canned ExportData."""

    def __init__(self, data: ExportData):
        self.data = data
        self.calls = 0

    def read_export(self) -> ExportData:
        self.calls += 1
        return self.data
