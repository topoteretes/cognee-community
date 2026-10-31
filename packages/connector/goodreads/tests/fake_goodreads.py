"""A realistic fake Goodreads library export for tests: a real CSV file, no network.

The CSV shape mirrors a real export downloaded from goodreads.com
(Settings -> Import & Export -> "Export Library"):

  - a UTF-8 BOM and a header matched by name (case-insensitive), with an extra
    unknown column Goodreads might add ("My New Column");
  - re-reads repeat the read-tracking columns: the default fixture appends a
    second, unnumbered ``Date Read`` / ``My Review`` / ``My Rating`` group;
    ``SNAKE_CASE_COLUMNS`` renames the families ``date_read`` / ``my_review`` /
    ``my_rating`` (plus numbered ``date_read 2`` ...), the other real shape;
  - ISBN/ISBN13 escaped as ``="..."``; reviews containing ``<br/>`` and links;
  - a read book with rating + review, a re-read book (dates out of order in
    the file), a currently-reading book, a to-read book, an unrated book
    (My Rating 0), a book with Additional Authors, custom shelves, a book read
    3 times with a single Date Read, a metadata footer junk line and a
    trailing empty line.
"""

import csv
import io
from pathlib import Path

DDIA = "1001"
PRAGMATIC = "1002"
SRE = "1003"
SYS_PERF = "1004"
K_AND_R = "1005"
CLEAN_CODE = "1006"
DUNE = "1007"

DDIA_REVIEW = (
    "A masterwork on systems design.<br/>"
    '<a href="https://example.com/ddia-notes">Full notes here</a>'
)
EDITED_DDIA_REVIEW = (
    "Rewritten after a re-read: even better than I remembered.<br/>"
    '<a href="https://example.com/ddia-notes">Notes updated</a>'
)
PRAGMATIC_REVIEW = "Good, but dated in places."

COLUMNS = [
    "Book Id",
    "Title",
    "Author",
    "Author l-f",
    "Additional Authors",
    "ISBN",
    "ISBN13",
    "My Rating",
    "Average Rating",
    "Publisher",
    "Binding",
    "Number of Pages",
    "Year Published",
    "Original Publication Year",
    "Date Read",
    "Date Added",
    "Bookshelves",
    "Bookshelves with positions",
    "Exclusive Shelf",
    "My Review",
    "Spoiler",
    "Private Notes",
    "Read Count",
    "Owned Copies",
    "My New Column",
    # the re-read book's second read: repeated, unnumbered read-tracking columns
    "Date Read",
    "My Review",
    "My Rating",
]

SNAKE_CASE_COLUMNS = [
    "book_id",
    "Title",
    "Author",
    "Author l-f",
    "Additional Authors",
    "ISBN",
    "ISBN13",
    "my_rating",
    "Average Rating",
    "Publisher",
    "Binding",
    "Number of Pages",
    "Year Published",
    "Original Publication Year",
    "date_read",
    "Date Added",
    "Bookshelves",
    "Bookshelves with positions",
    "Exclusive Shelf",
    "my_review",
    "Spoiler",
    "Private Notes",
    "Read Count",
    "Owned Copies",
    "My New Column",
    "date_read 2",
    "my_review 2",
    "my_rating 2",
]

# the fake's row keys, positionally aligned with COLUMNS / SNAKE_CASE_COLUMNS
_ROW_KEYS = [
    "book_id",
    "title",
    "author",
    "author_lf",
    "additional_authors",
    "isbn",
    "isbn13",
    "my_rating",
    "average_rating",
    "publisher",
    "binding",
    "pages",
    "year_published",
    "original_publication_year",
    "date_read",
    "date_added",
    "bookshelves",
    "bookshelves_with_positions",
    "exclusive_shelf",
    "my_review",
    "spoiler",
    "private_notes",
    "read_count",
    "owned_copies",
    "extra",
    "date_read_2",
    "my_review_2",
    "my_rating_2",
]


def book(**fields) -> list[str]:
    """One CSV record as a value list aligned with ``COLUMNS``."""
    return [str(fields.get(key, "")) for key in _ROW_KEYS]


def default_rows() -> list[list[str]]:
    return [
        book(
            book_id=DDIA,
            title="Designing Data-Intensive Applications",
            author="Martin Kleppmann",
            author_lf="Kleppmann, Martin",
            isbn='="1449373320"',
            isbn13='="9781449373320"',
            my_rating="5",
            average_rating="4.72",
            publisher="O'Reilly Media",
            binding="Paperback",
            pages="616",
            year_published="2017",
            original_publication_year="2015",
            date_read="2023/01/15",
            date_added="2022/12/01",
            bookshelves="favorites; systems",
            exclusive_shelf="read",
            my_review=DDIA_REVIEW,
            private_notes="Re-read chapters 5 and 6 annually.",
            read_count="1",
            owned_copies="1",
        ),
        book(
            book_id=PRAGMATIC,
            title="The Pragmatic Programmer",
            author="David Thomas",
            author_lf="Thomas, David",
            additional_authors="Andrew Hunt",
            my_rating="3",
            average_rating="4.29",
            date_read="2021/05/10",
            date_added="2019/02/20",
            exclusive_shelf="read",
            my_review=PRAGMATIC_REVIEW,
            read_count="2",
            date_read_2="2019/03/01",
            my_rating_2="4",
        ),
        book(
            book_id=SRE,
            title="Site Reliability Engineering",
            author="Betsy Beyer",
            author_lf="Beyer, Betsy",
            my_rating="0",
            average_rating="4.36",
            binding="Hardcover",
            pages="524",
            year_published="2016",
            original_publication_year="2016",
            date_added="2025/11/01",
            exclusive_shelf="currently-reading",
            read_count="0",
        ),
        book(
            book_id=SYS_PERF,
            title="Systems Performance",
            author="Brendan Gregg",
            author_lf="Gregg, Brendan",
            my_rating="0",
            average_rating="4.55",
            year_published="2020",
            original_publication_year="2013",
            date_added="2025/10/20",
            exclusive_shelf="to-read",
            read_count="0",
        ),
        book(
            book_id=K_AND_R,
            title="The C Programming Language",
            author="Brian Kernighan",
            author_lf="Kernighan, Brian",
            additional_authors="Dennis Ritchie",
            my_rating="0",
            average_rating="4.44",
            publisher="Prentice Hall",
            pages="272",
            year_published="1988",
            original_publication_year="1978",
            date_read="2020/09/09",
            date_added="2020/08/01",
            exclusive_shelf="read",
            read_count="1",
        ),
        book(
            book_id=CLEAN_CODE,
            title="Clean Code",
            author="Robert C. Martin",
            author_lf="Martin, Robert C.",
            isbn="0132350882",
            isbn13='="9780132350884"',
            my_rating="4",
            average_rating="4.29",
            binding="Paperback",
            pages="464",
            year_published="2008",
            original_publication_year="2008",
            date_read="2022/08/20",
            date_added="2022/07/15",
            bookshelves="work",
            exclusive_shelf="read",
            read_count="3",
            owned_copies="0",
        ),
        book(
            book_id=DUNE,
            title="Dune",
            author="Frank Herbert",
            author_lf="Herbert, Frank",
            my_rating="0",
            average_rating="4.26",
            bookshelves="sci-fi; favorites",
            exclusive_shelf="to-read",
            date_added="2024/01/05",
            read_count="0",
        ),
        book(extra="Generated by Goodreads"),  # metadata footer: no Book Id, no Title
        book(),  # the trailing empty line
    ]


def csv_text(columns: list[str], rows: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def write_export(
    path: Path,
    rows: list[list[str]] | None = None,
    columns: list[str] | None = None,
    bom: bool = True,
) -> Path:
    """Write the fixture (or an override) as a real export CSV."""
    text = csv_text(columns or COLUMNS, default_rows() if rows is None else rows)
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")
    return path


# ------------------------------------------------------------- export variants
def with_edited_review(rows: list[list[str]]) -> list[list[str]]:
    """A newer export whose DDIA review was rewritten."""
    out = []
    for row in rows:
        if row and row[0] == DDIA:
            row = list(row)
            row[_ROW_KEYS.index("my_review")] = EDITED_DDIA_REVIEW
        out.append(row)
    return out


def without_book(rows: list[list[str]], book_id: str) -> list[list[str]]:
    """A newer export from which one book vanished entirely."""
    return [row for row in rows if not row or row[0] != book_id]


def with_bookshelves(rows: list[list[str]], book_id: str, bookshelves: str) -> list[list[str]]:
    """A newer export in which a book moved between custom shelves."""
    out = []
    for row in rows:
        if row and row[0] == book_id:
            row = list(row)
            row[_ROW_KEYS.index("bookshelves")] = bookshelves
        out.append(row)
    return out


def with_long_title(rows: list[list[str]]) -> list[list[str]]:
    """An export with a book whose title is longer than 80 characters."""
    return [
        *rows[:-2],
        book(
            book_id="1008",
            title="A" * 120,
            author="Some Author",
            author_lf="Author, Some",
            exclusive_shelf="read",
            read_count="1",
        ),
        rows[-2],
        rows[-1],
    ]


class FakeGoodreadsService:
    """Service injection without a filesystem: returns canned parsed rows."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls = 0

    def read_export(self) -> list[dict]:
        self.calls += 1
        return self.rows
