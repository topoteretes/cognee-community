"""Turn Search Analytics rows into the text documents cognee reads.

Documents are deterministic: nothing volatile (today's date, relative times) is rendered, so an
unchanged period renders to the same bytes and cognee's content hash skips the LLM for it.
"""

import re
from dataclasses import dataclass
from datetime import date

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
# Query strings and URLs come from the public web: keep each one short and on one line.
_MAX_TEXT = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

_OMITTED_NOTE = (
    "Search Console reports the top rows by clicks and leaves out rare queries for privacy, so "
    "the rows listed here do not add up to the totals."
)


@dataclass(frozen=True)
class PeriodInfo:
    """Which month of which property a document describes."""

    site_url: str
    period: str  # YYYY-MM
    first: date
    end: date  # the last day with final data, which can be before the month's end
    search_type: str

    @property
    def header(self) -> str:
        return (
            f"Dates: {self.first} to {self.end}. Search type: {self.search_type}. Final data only."
        )


def period_label(period: str) -> str:
    """``2026-09`` as ``September 2026``."""
    year, month = period.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


def site_label(site_url: str) -> str:
    """A property as people name it: ``sc-domain:example.com`` and ``https://example.com/``."""
    return site_url.removeprefix("sc-domain:").split("://", 1)[-1].rstrip("/") or site_url


def metrics(row: dict) -> str:
    """One row's numbers on a single line."""
    return (
        f"{int(row.get('clicks', 0)):,} clicks, {int(row.get('impressions', 0)):,} impressions, "
        f"CTR {float(row.get('ctr', 0)) * 100:.2f}%, average position "
        f"{float(row.get('position', 0)):.1f}"
    )


def overview_content(info: PeriodInfo, data: dict) -> str:
    """A month of a property: totals, devices, top countries, top queries and top pages."""
    lines = [
        f"Google Search Console performance of {info.site_url} in {period_label(info.period)}.",
        info.header,
        "",
        f"Totals: {metrics(data['totals'])}.",
    ]
    if data["devices"]:
        lines += ["", "By device:"]
        lines += [f"- {_text(row['keys'][0]).title()}: {metrics(row)}" for row in data["devices"]]
    if data["countries"]:
        lines += ["", "Top countries:"]
        lines += [f"- {_text(row['keys'][0]).upper()}: {metrics(row)}" for row in data["countries"]]
    if data["queries"]:
        lines += ["", "Top search queries:"]
        lines += [
            f'{n}. "{_text(row["keys"][0])}": {metrics(row)}' for n, row in _ranked(data["queries"])
        ]
    if data["pages"]:
        lines += ["", "Top pages:"]
        lines += [
            f"{n}. {_text(row['keys'][0])}: {metrics(row)}" for n, row in _ranked(data["pages"])
        ]
    lines += ["", _OMITTED_NOTE]
    return "\n".join(lines)


def page_content(info: PeriodInfo, page: dict, queries: list[dict]) -> str:
    """One page in a month, with the queries that led to it."""
    url = _text(page["keys"][0])
    lines = [
        f"Google Search Console performance of the page {url} on {info.site_url} in "
        f"{period_label(info.period)}.",
        info.header,
        "",
        f"Page totals: {metrics(page)}.",
    ]
    if queries:
        lines += ["", "Search queries that led to this page:"]
        # A page + query row has the page first, then the query.
        lines += [f'{n}. "{_text(row["keys"][1])}": {metrics(row)}' for n, row in _ranked(queries)]
    lines += ["", _OMITTED_NOTE]
    return "\n".join(lines)


def _ranked(rows: list[dict]):
    return enumerate(rows, start=1)


def _text(value: str) -> str:
    """One line of untrusted text, capped in length."""
    clean = _CONTROL.sub(" ", str(value)).strip()
    clean = re.sub(r"\s{2,}", " ", clean)
    return clean if len(clean) <= _MAX_TEXT else clean[: _MAX_TEXT - 1] + "…"
