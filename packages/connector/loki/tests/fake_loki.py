"""An in-memory stand-in for Loki's query_range, with Loki's paging behaviour."""

import re

MINUTE = 60 * 1_000_000_000
# 2026-01-01T00:00:00Z
T0 = 1_767_225_600 * 1_000_000_000

_MATCHER = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


class FakeLoki:
    """Holds streams of lines and answers ``query_range`` like Loki does.

    Lines are returned oldest first, at most ``limit`` per answer, grouped by stream. A
    selector matches streams whose labels include every ``label="value"`` pair in it; a
    ``|= "text"`` filter keeps lines containing the text.
    """

    def __init__(self, *, cut_per_stream: bool = False):
        # cut_per_stream: a full answer keeps each stream's oldest lines in equal shares,
        # as Loki can when a query is split, instead of the oldest lines overall
        self.cut_per_stream = cut_per_stream
        self.streams: list[tuple[dict, list[tuple[int, str]]]] = []
        self.calls: list[tuple[str, int, int, int]] = []
        self.fail_with: Exception | None = None

    def push(self, labels: dict, ts: int, line: str) -> None:
        for stream_labels, lines in self.streams:
            if stream_labels == labels:
                lines.append((ts, line))
                return
        self.streams.append((dict(labels), [(ts, line)]))

    def query_range(self, query: str, start: int, end: int, limit: int) -> list[dict]:
        self.calls.append((query, start, end, limit))
        if self.fail_with is not None:
            raise self.fail_with
        selector, _, line_filter = query.partition("|=")
        wanted = dict(_MATCHER.findall(selector))
        needle = line_filter.strip().strip('"')
        entries = []
        for index, (labels, lines) in enumerate(self.streams):
            if any(labels.get(key) != value for key, value in wanted.items()):
                continue
            for ts, line in lines:
                if start <= ts < end and needle in line:
                    entries.append((ts, index, line))
        entries.sort()
        if self.cut_per_stream and len(entries) > limit:
            share = -(-limit // len({index for _, index, _ in entries}))
            kept: dict[int, int] = {}
            cut = []
            for entry in entries:
                if kept.get(entry[1], 0) < share:
                    kept[entry[1]] = kept.get(entry[1], 0) + 1
                    cut.append(entry)
            # a full answer is always full: top it up when a stream ran out early
            cut += [entry for entry in entries if entry not in cut][: max(0, limit - len(cut))]
            entries = sorted(cut)
        grouped: dict[int, list] = {}
        for ts, index, line in entries[:limit]:
            grouped.setdefault(index, []).append([str(ts), line])
        return [{"stream": self.streams[i][0], "values": values} for i, values in grouped.items()]
