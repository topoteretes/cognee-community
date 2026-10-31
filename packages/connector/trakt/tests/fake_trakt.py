"""An in-memory Trakt API for tests (no token, no network).

The payloads mirror the real response shapes from the Trakt API reference: a
movie watched twice, a show with two seasons watched on different dates,
ratings (including a rated-but-never-watched movie and show), a watchlist, one
personal list with mixed media whose items are paginated across two pages, a
comment, and the ``/sync/last_activities`` snapshot that moves one section at
a time between runs.
"""

import copy
import math

from cognee_community_connector_trakt.trakt import (
    TraktAuthError,
    TraktRateLimitError,
    _TraktResponse,
)

T0 = "2020-01-01T00:00:00.000Z"
T1 = "2025-01-01T00:00:00.000Z"
T2 = "2025-06-01T00:00:00.000Z"
T3 = "2026-02-01T00:00:00.000Z"

SETTINGS = {
    "user": {
        "username": "demo",
        "private": False,
        "name": "Demo User",
        "vip": False,
        "vip_ep": False,
        "ids": {"slug": "demo", "trakt": 999, "uuid": "ca82ca82"},
        "joined_at": T0,
    }
}

INCEPTION = {
    "title": "Inception",
    "year": 2010,
    "ids": {"trakt": 1, "slug": "inception-2010", "imdb": "tt1375666", "tmdb": 27205},
    "tagline": "Your mind is the scene of the crime.",
    "overview": "A thief who steals corporate secrets through dream-sharing technology.",
    "runtime": 148,
    "released": "2010-07-16",
    "rating": 8.35,
    "votes": 47000,
    "comment_count": 280,
    "trailer": "https://youtube.com/watch?v=YoHD9XEInc0",
    "homepage": "https://www.warnerbros.com/inception",
    "updated_at": T1,
}
OLD_BOY = {
    "title": "Oldboy",
    "year": 2003,
    "ids": {"trakt": 2, "slug": "oldboy-2003", "imdb": "tt1192349"},
    "overview": "After being kidnapped and imprisoned for fifteen years, Oh Dae-su is released.",
    "runtime": 120,
    "released": "2003-11-21",
}
TENET = {
    "title": "Tenet",
    "year": 2020,
    "ids": {"trakt": 3, "slug": "tenet-2020", "imdb": "tt6723592"},
    "overview": "Armed with only one word, Tenet, and fighting for the survival of the world.",
    "runtime": 150,
    "released": "2020-08-26",
}
SEVERANCE = {
    "title": "Severance",
    "year": 2022,
    "ids": {"trakt": 10, "slug": "severance", "tvdb": 372956, "imdb": "tt11280740"},
    "aired_episodes": 19,
    "overview": "Mark leads a team of office workers whose memories have been surgically "
    "divided between their work and personal lives.",
}
DARK = {
    "title": "Dark",
    "year": 2017,
    "ids": {"trakt": 11, "slug": "dark", "tvdb": 331631},
    "overview": "A missing child sets four families on a frantic hunt for answers.",
    "aired_episodes": 26,
}

# Watched movies: inception twice, tenet once (unrated), oldboy never.
WATCHED_MOVIES = [
    {
        "plays": 2,
        "last_watched_at": "2024-11-02T21:10:00.000Z",
        "last_updated_at": "2024-11-02T21:12:00.000Z",
        "movie": INCEPTION,
    },
    {
        "plays": 1,
        "last_watched_at": "2025-03-10T19:00:00.000Z",
        "last_updated_at": "2025-03-10T19:02:00.000Z",
        "movie": TENET,
    },
]

# Watched shows: severance, two seasons, episodes watched on different dates
# (listing order is by recency, not by watch date, to prove the timeline sorts).
EPISODES_SEVERANCE = {
    (1, 1): (101, "Good News About Hell"),
    (1, 2): (102, "Half Loop"),
    (2, 1): (201, "Hello, Ms. Huang"),
    (2, 4): (204, "Woe's Hollow"),
}
WATCHED_SHOWS = [
    {
        "plays": 40,
        "last_watched_at": "2026-01-15T23:00:00.000Z",
        "last_updated_at": "2026-01-15T23:01:00.000Z",
        "reset_at": None,
        "show": SEVERANCE,
        "seasons": [
            {
                "number": 1,
                "episodes": [
                    {"number": 1, "plays": 1, "last_watched_at": "2025-02-01T20:00:00.000Z"},
                    {"number": 2, "plays": 1, "last_watched_at": "2025-02-03T20:00:00.000Z"},
                ],
            },
            {
                "number": 2,
                # Listed newest-first (by recency), not by watch date, to
                # prove the rendered timeline is sorted by date.
                "episodes": [
                    {"number": 1, "plays": 1, "last_watched_at": "2026-01-15T23:00:00.000Z"},
                    {"number": 4, "plays": 2, "last_watched_at": "2025-12-20T21:00:00.000Z"},
                ],
            },
        ],
    }
]

RATINGS = {
    "movies": [
        {"rated_at": "2021-06-01T12:00:00.000Z", "rating": 9, "type": "movie", "movie": INCEPTION},
        {"rated_at": "2020-02-02T12:00:00.000Z", "rating": 7, "type": "movie", "movie": OLD_BOY},
    ],
    "shows": [
        {"rated_at": "2024-01-01T12:00:00.000Z", "rating": 6, "type": "show", "show": DARK},
    ],
    "seasons": [],
    "episodes": [
        {
            "rated_at": "2026-01-16T12:00:00.000Z",
            "rating": 8,
            "type": "episode",
            "episode": {
                "season": 2,
                "number": 1,
                "title": "Hello, Ms. Huang",
                "ids": {"trakt": 201, "tvdb": 111},
            },
            "show": SEVERANCE,
        }
    ],
}

WATCHLIST = [
    {
        "rank": 1,
        "id": 901,
        "listed_at": "2025-05-01T10:00:00.000Z",
        "type": "movie",
        "movie": TENET,
    },
    {
        "rank": 2,
        "id": 902,
        "listed_at": "2026-01-20T10:00:00.000Z",
        "type": "episode",
        "episode": {
            "season": 2,
            "number": 4,
            "title": "Woe's Hollow",
            "ids": {"trakt": 204, "tvdb": 114},
        },
        "show": SEVERANCE,
    },
]

LIST_REWATCHABLES = {
    "name": "Rewatchables",
    "description": "Movies and shows I keep coming back to.",
    "privacy": "private",
    "share_link": "https://trakt.tv/lists/100",
    "type": "personal",
    "display_numbers": True,
    "allow_comments": True,
    "sort_by": "rank",
    "sort_how": "asc",
    "created_at": T0,
    "updated_at": T1,
    "item_count": 3,
    "comment_count": 0,
    "likes": 0,
    "ids": {"trakt": 100, "slug": "rewatchables"},
    "user": SETTINGS["user"],
}
LIST_ITEMS_REWATCHABLES = [
    {
        "rank": 1,
        "id": 801,
        "listed_at": "2021-05-01T10:00:00.000Z",
        "type": "movie",
        "movie": INCEPTION,
    },
    {
        "rank": 2,
        "id": 802,
        "listed_at": "2021-05-02T10:00:00.000Z",
        "type": "show",
        "show": SEVERANCE,
    },
    {
        "rank": 3,
        "id": 803,
        "listed_at": "2026-02-01T10:00:00.000Z",
        "type": "episode",
        "episode": {
            "season": 2,
            "number": 4,
            "title": "Woe's Hollow",
            "ids": {"trakt": 204, "tvdb": 114},
        },
        "show": SEVERANCE,
    },
]

COMMENTS = {
    "movies": [
        {
            "type": "movie",
            "movie": INCEPTION,
            "comment": {
                "id": 555,
                "parent_id": 0,
                "created_at": "2024-11-03T09:00:00.000Z",
                "updated_at": "2024-11-03T09:00:00.000Z",
                "comment": "Rewatched with the kids; the hallway scene still holds up.",
                "spoiler": False,
                "review": False,
                "replies": 0,
                "likes": 2,
                "user_rating": 9,
                "user": SETTINGS["user"],
            },
        }
    ],
    "shows": [],
}


def _seasons_payload(show, episodes):
    """A /shows/:id/seasons payload: one season per season number."""
    by_season: dict[int, list] = {}
    for (season_number, episode_number), (trakt_id, title) in episodes.items():
        by_season.setdefault(season_number, []).append(
            {
                "season": season_number,
                "number": episode_number,
                "title": title,
                "ids": {"trakt": trakt_id, "tvdb": trakt_id + 1000},
            }
        )
    return [
        {
            "number": number,
            "ids": {"trakt": number + 900, "tvdb": number + 5000},
            "episode_count": len(items),
            "episodes": items,
        }
        for number, items in sorted(by_season.items())
    ]


def default_activities():
    return {
        "all": T1,
        "movies": {
            "watched_at": T1,
            "collected_at": T0,
            "rated_at": T1,
            "watchlisted_at": T1,
            "favorited_at": T0,
            "commented_at": T1,
            "paused_at": T0,
            "hidden_at": T0,
        },
        "shows": {
            "rated_at": T0,
            "watchlisted_at": T0,
            "favorited_at": T0,
            "commented_at": T0,
            "hidden_at": T0,
        },
        "seasons": {"rated_at": T0, "watchlisted_at": T0, "commented_at": T0},
        "episodes": {
            "watched_at": T1,
            "collected_at": T0,
            "rated_at": T1,
            "watchlisted_at": T1,
            "commented_at": T0,
            "paused_at": T0,
        },
        "comments": {"liked_at": T0, "reacted_at": T0, "blocked_at": T0},
        "lists": {"liked_at": T0, "updated_at": T1},
        "watchlist": {"updated_at": T1},
    }


class FakeTrakt:
    """In-memory Trakt API: serves the canned payloads, records every request,
    and can mutate its history and activity timestamps between runs.

    The list-items endpoint is paginated (``page_size`` per page, with the real
    ``X-Pagination-*`` headers); every other endpoint behaves like the real
    unpaginated ones and returns the whole list without pagination headers.
    """

    def __init__(self, username="demo"):
        self.username = username
        self.watched_movies = [dict(entry) for entry in WATCHED_MOVIES]
        self.watched_shows = [
            {
                **entry,
                "seasons": [
                    {**season, "episodes": list(season["episodes"])}
                    for season in entry["seasons"]
                ],
            }
            for entry in WATCHED_SHOWS
        ]
        self.ratings = {t: [dict(item) for item in items] for t, items in RATINGS.items()}
        self.watchlist = [dict(item) for item in WATCHLIST]
        self.lists = [dict(LIST_REWATCHABLES)]
        self.list_items = {100: [dict(item) for item in LIST_ITEMS_REWATCHABLES]}
        self.comments = {t: [dict(item) for item in items] for t, items in COMMENTS.items()}
        self.activities = default_activities()
        self.page_size = 2  # the list's 3 items need two pages
        self.requests_log: list[tuple[str, dict]] = []
        self.fail_once: tuple[str, float] | None = None  # ("rate_limit", seconds) or ("auth", 0)

    # -- test-side mutations -------------------------------------------------
    def unwatch_movie(self, trakt_id: int) -> None:
        """Remove a movie from history: gone from the listing, watched_at moves."""
        self.watched_movies = [
            entry for entry in self.watched_movies if entry["movie"]["ids"]["trakt"] != trakt_id
        ]
        self.bump("movies", "watched_at")

    def unwatch_show_season(self, show_trakt_id: int, season: int) -> None:
        """Remove one season's episodes from a show's history."""
        for entry in self.watched_shows:
            if entry["show"]["ids"]["trakt"] == show_trakt_id:
                entry["seasons"] = [s for s in entry["seasons"] if s["number"] != season]
        self.bump("episodes", "watched_at")

    def bump(self, section: str, field: str = "updated_at", value: str = T3) -> None:
        self.activities.setdefault(section, {})[field] = value
        self.activities["all"] = value

    def paths(self) -> list[str]:
        return [path for path, _params in self.requests_log]

    def params_of(self, path: str) -> list[dict]:
        return [params for called, params in self.requests_log if called == path]

    # -- the client seam ------------------------------------------------------
    def request(self, path: str, params: dict | None = None):
        self.requests_log.append((path, dict(params or {})))
        if self.fail_once is not None:
            failure, detail = self.fail_once
            self.fail_once = None
            if failure == "rate_limit":
                raise TraktRateLimitError("Trakt asked to wait.", detail)
            raise TraktAuthError("Trakt rejected the access token.")
        params = params or {}
        page = int(params.get("page", 1))
        limit = int(params.get("limit", 10))
        if path == "/users/me/settings":
            user = {**SETTINGS["user"], "username": self.username}
            return _TraktResponse({**SETTINGS, "user": user}, {})
        if path == "/sync/last_activities":
            # Deep copy, like a freshly parsed response: a test mutating the
            # fake's activity timestamps must not also mutate the snapshot an
            # earlier run handed to the sync state.
            return _TraktResponse(copy.deepcopy(self.activities), {})
        if path == "/users/me/watched/movies":
            return _TraktResponse(list(self.watched_movies), {})
        if path == "/users/me/watched/shows":
            return _TraktResponse(list(self.watched_shows), {})
        if path.startswith("/sync/ratings/"):
            media_type = path.removeprefix("/sync/ratings/")
            return _TraktResponse(list(self.ratings.get(media_type, [])), {})
        if path == "/sync/watchlist":
            return _TraktResponse(list(self.watchlist), {})
        if path == "/users/me/lists":
            return _TraktResponse(list(self.lists), {})
        if path.startswith("/users/me/lists/") and path.endswith("/items"):
            list_id = path.removeprefix("/users/me/lists/").removesuffix("/items")
            return self._paged(list(self.list_items.get(int(list_id), [])), page, limit)
        if path.startswith("/users/me/comments/"):
            media_type = path.removeprefix("/users/me/comments/")
            return _TraktResponse(list(self.comments.get(media_type, [])), {})
        if path.startswith("/shows/") and path.endswith("/seasons"):
            show_id = int(path.removeprefix("/shows/").removesuffix("/seasons"))
            if show_id == SEVERANCE["ids"]["trakt"]:
                return _TraktResponse(_seasons_payload(SEVERANCE, EPISODES_SEVERANCE), {})
            return _TraktResponse([], {})
        raise AssertionError(f"unexpected path {path!r}")

    def _paged(self, items, page, limit):
        """Serve one page with the real pagination headers."""
        effective = max(1, min(limit, self.page_size))
        chunk = items[(page - 1) * effective : page * effective]
        page_count = max(1, math.ceil(len(items) / effective))
        return _TraktResponse(
            chunk,
            {
                "X-Pagination-Page": str(page),
                "X-Pagination-Limit": str(effective),
                "X-Pagination-Page-Count": str(page_count),
                "X-Pagination-Item-Count": str(len(items)),
            },
        )
