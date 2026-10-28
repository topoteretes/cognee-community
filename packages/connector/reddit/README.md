# cognee-community-connector-reddit

A Reddit data-source connector for [cognee](https://github.com/topoteretes/cognee): turn the
discussions of the subreddits you follow or moderate into memory, so you can ask "what did
people recommend for packaging a CLI last month?" across every thread at once.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every post becomes one document
(its subreddit, author, flair, date and text, then its comment thread), flowing through
cognee's normal cognify entity extraction, in the node set of its subreddit.

## Install

```bash
uv pip install cognee-community-connector-reddit
# or, from this monorepo:
cd packages/connector/reddit && uv sync
```

## Setup

The connector uses Reddit's OAuth API through a **script** app.

1. Reddit approves Data API access before an account can create apps: at
   https://www.reddit.com/prefs/apps an account without access is pointed to the
   [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
   and its access request form.
2. Once approved, click **create another app...** on that page, choose **script**, and set the
   redirect URI to `http://localhost:8080`. The string under the app's name is its client id,
   and the one next to **secret** its secret.
3. Pass them as `client_id=` and `client_secret=`, with a descriptive `user_agent=` in the form
   Reddit asks for, `<platform>:<app id>:<version> (by /u/<your username>)`. The example reads
   them from `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and `REDDIT_USER_AGENT`.

By default the connector signs in with app-only OAuth (`client_credentials`), which reads
public subreddits and needs no Reddit password. To read a private subreddit your account is a
member of, also pass `username=` and `password=` of the account that owns the app.

## Usage

```python
import os

import cognee
from cognee_community_connector_reddit import reddit_source

await cognee.remember(
    reddit_source(
        subreddits=["python", "learnpython"],
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent="script:cognee-reddit:0.1 (by /u/your_name)",
    ),
    dataset_name="reddit",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("How do people package CLIs?", datasets=["reddit"])
```

Run the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `subreddits` | required | Subreddits to sync, by name (`python` or `r/python`). A subreddit that cannot be read on its first sync raises (most likely a typo). |
| `include_comments` | `True` | Ingest each post's comment thread with it. |
| `max_comment_depth` | `8` | Deepest reply level read, 0 being a top-level comment. Reddit itself cuts threads at depth 10. |
| `max_more_requests` | `5` | `/api/morechildren` requests per thread, each loading up to 100 hidden comments. |
| `refresh_days` | `7` | Posts newer than this are read again when their text, edit time or comment count changes. Older posts are only checked for deletion. |
| `first_sync_posts` | `100` | Newest posts read from each subreddit on its first sync. |
| `max_requests` | `100` | Request budget of one run, Reddit's per-minute limit. A bigger sync finishes over several runs. |
| `resource_name` | derived | Staging table, which scopes this sync's state and cleanup: lowercase letters, digits and underscores, up to 64. Derived from the subreddits (`reddit_learnpython_python_<hash>`), so two syncs into one dataset never delete each other's posts. |

## What a post looks like

```
# Packaging a CLI in 2026

Subreddit: r/Python
Author: u/alice
Flair: Help
Posted: 2026-10-20

What do people use to ship a CLI these days?

Comments:
- u/bob: uv tool install works well.
  - u/alice: Thanks, trying it.
- [deleted]
  - u/carol: pipx still does the job.
```

The document's title is the post's title, its URL the post's permalink, and its node set
`r/<subreddit>`, so `recall` can be scoped to one subreddit. A link post also shows its link.
Scores, upvotes and other counters are left out, so votes alone never re-process a post. A
deleted or removed comment is left out, or shown as `[deleted]` when it has replies. A thread
whose comments were not all loaded (deeper than `max_comment_depth`, "continue this thread",
or more hidden comments than `max_more_requests` loads) ends with
`(Some comments of this thread were not loaded.)`.

## How sync and forget-on-delete work

- **New posts** come from the subreddit's `/new` listing with the `before` cursor, anchored on
  the newest post seen. When the page is full, or comes back empty while the anchor post is
  gone, the connector pages back with `after` to the anchor's time, so no post is skipped. The
  first sync reads the newest `first_sync_posts` posts; Reddit's listings reach back about
  1,000 posts.
- **Changes and deletions** of synced posts are read from `/api/info`, 100 posts a request,
  picking up from where the last run stopped, so every post gets its turn. A post newer than
  `refresh_days` whose text, flair, link, edit time or comment count changed has its thread
  read again; a thread whose text comes out the same is not emitted again.
- **Forget-on-delete.** A post is removed from memory when Reddit reports it removed
  (`removed_by_category` set, or its text `[removed]`) or deleted (`[deleted]`), when
  `/api/info` no longer returns it, when its thread answers 404, and when its subreddit is
  no longer selected. A post that only drops off the `/new` listing stays, and so does a post
  whose author deleted their account (its text is still there).
- **Nothing is forgotten by mistake.** A subreddit that answers 403 or 404, or redirects (it
  does not exist), on a later sync is left as it is, counted in `unreadable`, and its posts are
  not checked that run, since a private subreddit's posts disappear from `/api/info` too. An
  `/api/info` answer that holds none of the posts asked for forgets nothing until the next
  run confirms it.
- **A run is bounded.** `max_requests` counts every request and is checked once a listing, a
  check or a thread is finished, so every run makes progress; the threads a run did not reach
  are kept in state and read first by the next. Reddit's rate limit headers are followed: a
  used-up window, a `Retry-After`, 429s and 5xx are waited out up to 60 seconds each and five
  minutes a run. A longer wait stops the run with `failed` and `failed_rate_limit`, and a token
  rejected after the first request (revoked mid-run) with `failed` and `failed_auth`. A spent
  budget is a planned pause and sets only `failed_budget`. Credentials rejected on the first
  request raise, and any other error raises so dlt keeps the previous state.

Two limits to know: comment edits, and comments deleted after a thread was read, show only once
the post itself changes or gets new comments within `refresh_days`, since `/api/info` reports
no change for them. And a post older than `refresh_days` keeps the text it had then.

`cognee_sync_stats` always carries `scanned`, `emitted`, `unchanged`, `deleted` and `gone`
posts, `unreadable` subreddits and threads, `truncated` threads, `skipped` malformed listing
entries, and `failed` plus `failed_auth`, `failed_budget` and `failed_rate_limit`.

## Privacy

This reads public posts and comments of the selected subreddits, with their authors'
usernames, and with `username=` the private subreddits that account can see. Nothing is
fetched until you call `remember`, and anyone with read access to the target dataset can read
what was ingested. The connector only reads: it never posts, comments, votes or messages. The
client secret and password are only sent to `www.reddit.com` for a token, the token only to
`oauth.reddit.com`, and none of them is written into documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no Reddit account. The fake API in `tests/conftest.py` builds its answers from
real, recorded Reddit responses in `tests/fixtures/` (taken from PRAW's test cassettes, see
the README there): `/comments/<id>` answering two listings, `/api/morechildren` answering a
flat list, "continue this thread" stubs, `/api/info` leaving out what it cannot find, and a
quarantined subreddit's 403. The tests run the client against a mocked HTTP transport (both
grants, token renewal, rate limit headers, retries, redirects not followed, errors without
secrets), check rendering on the recorded thread, drive the sync (the `before` cursor and its
fallbacks, edits, comments, votes, the refresh window, every deletion case, unreadable
subreddits, `more` expansion and truncation, budget, rate limit and revoked-token stops),
run it through a real dlt pipeline, and run `cognee.remember` end to end to prove a removed
post leaves the graph.
