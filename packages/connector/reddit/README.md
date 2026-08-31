# cognee-community-connector-reddit

A Reddit data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync subreddit submissions and their comment trees into memory — "ask my subreddits".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each submission is
ingested as **one normal document** (title + body + rendered comment tree), so it flows
through cognee's cognify entity-extraction pipeline rather than the deterministic
dlt-row path, via cognee's document-mode marker. Auth is **OAuth 2.0 with a script
app**; the API is spoken over the standard library (`urllib`) — no `praw`/`asyncpraw`
dependency.

## Requirements

- cognee **≥ 1.4.0** — document mode (`DOCUMENT_SOURCE_ATTR` + `resolve_dlt_sources`)
  first shipped there; on older versions rows would be treated as dlt schema rows.
- A Reddit account that can create a script app.

## Install

```bash
uv pip install cognee-community-connector-reddit
# or, from this monorepo:
cd packages/connector/reddit && uv sync
```

## Setup — create a Reddit script app

1. Go to <https://www.reddit.com/prefs/apps> → **create another app...**
2. Pick type **script** (this is the grant the connector uses), give it a name, and put
   anything valid in *redirect uri* (e.g. `http://localhost:8080` — a script app never
   redirects anywhere).
3. The short string **under the app name** is the client id; the field labelled
   **secret** is the client secret.
4. Export the credentials — the connector reads them from the environment, so nothing
   ends up in your code:

   ```bash
   export REDDIT_CLIENT_ID="..."
   export REDDIT_CLIENT_SECRET="..."
   export REDDIT_USERNAME="your-reddit-user"
   export REDDIT_PASSWORD="..."
   # Reddit throttles generic user agents hard — be descriptive and identify yourself.
   export REDDIT_USER_AGENT="python:my-cognee-bot:0.1.0 (by /u/your-reddit-user)"
   ```

   Every one of these can also be passed directly to `reddit_source(...)`.

**Refresh-token grant.** If you already have a refresh token from a web/installed app,
pass `refresh_token=` (or set `REDDIT_REFRESH_TOKEN`) and the username/password are not
needed. The connector does **not** implement the interactive authorization-code dance
that mints such a token — that needs a browser round trip and a redirect listener, which
does not belong in a batch ingestion source.

## Usage

```python
import cognee
from cognee_community_connector_reddit import reddit_source

await cognee.remember(
    # "python", "r/Python" and "/r/python/" all work.
    # Omit the argument to fall back to the account's subscribed subreddits.
    reddit_source(["r/LocalLLaMA", "cognee"]),
    dataset_name="reddit",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by submission fullname
    max_rows_per_table=0,  # 0 = no row cap (busy subreddits exceed the default 50)
)

answer = await cognee.search(
    query_text="What are people saying about local inference on Apple silicon?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["reddit"],
)
```

See `examples/example.py` for the full flow.

## What gets ingested

- Submissions from `/r/<sub>/new` for every configured subreddit, **one document per
  submission**: the title, the selftext (or a note naming the outbound link for a link
  post), and the comment tree rendered as indented markdown bullets
  (`- **u/author** (N points): body`, two spaces per reply level).
- Subreddit, author, timestamp, score, comment count, flair, `is_self`, the permalink
  and the truncation flag become record metadata (a `metadata` JSON column, also folded
  into the text as a `Submission context` section so it survives entity extraction —
  which is what turns author→submission and commenter→thread into graph edges).
- The row id is the submission **fullname** (`t3_<id>`) — globally unique, and the same
  token the listing cursor and `/api/info` speak. The `url` is the real
  `https://www.reddit.com/r/<sub>/comments/...` permalink.
- Media is not downloaded; a link post carries the link, not its target.

## How incremental sync works

The cursor is the listing **`before`/`after`** cursor, kept in dlt's per-resource state
as `state["subreddits"][name]["newest"]` (the fullname of the newest submission seen):

- **First run** — backfills `/r/<sub>/new` paging forward with `after=`, up to
  `backfill_limit` submissions per subreddit (default 200).
- **Every later run** — pages `/r/<sub>/new?before=<newest fullname>`, so only
  submissions posted since the last run are fetched. The anchor only ever moves
  forward.
- **Refresh window** — a listing cursor answers "what is new", never "what changed", so
  the `refresh_limit` most recently ingested submissions per subreddit (default 25) are
  also re-rendered each run. That is what notices an edited body, a new reply, or a
  comment that disappeared. Their current payloads come from the delete re-check below,
  so a refresh costs one comment-tree call each and nothing more. Set `refresh_limit=0`
  to make the sync strictly new-submissions-only.
- **Re-emission gate** — a sha256 fingerprint of each submission's *semantic* material
  (title, body, subreddit, author, and the comment tree's authors/bodies/shape) is kept
  in state. An unchanged submission is not re-yielded, so nothing is re-cognified for
  free. Vote score and comment counts are deliberately **excluded** from the
  fingerprint: they churn on nearly every run and hashing them would re-ingest the whole
  corpus every time. The consequence is that a stored `score` is the score at last
  content change, not a live number.

## How forget-on-delete works — and its honest boundary

Reddit publishes no deletion feed, so the connector asks. Every run it re-checks the
submission ids it already knows through `/api/info?id=t3_a,t3_b,...` (100 ids per call)
and treats three answers as *gone*:

1. the id no longer comes back from `/api/info` at all,
2. it comes back with `removed_by_category` set (moderator / admin / spam removal), or
3. its author **and** its body are both `[deleted]`/`[removed]` (an author self-delete).

Each of those yields a `{"id": "t3_...", "_deleted": True}` row. dlt drops it on `merge`
via the hard-delete column, and cognee's existing `orphan_cleanup` purges it from the
graph, vector and relational stores — so **deleting the source upstream removes it from
the graph on the next sync**. Because a submission and its comments are one document,
forgetting the submission forgets its whole comment tree with it.

**The boundary, stated plainly.** A *comment* deleted inside a still-live submission is
not something Reddit signals. It is noticed the next time that submission is
re-rendered — immediately if it is still inside the refresh window, otherwise not until
the submission is re-rendered for some other reason. So comment-level deletion is
eventually consistent, not instant, and the tests assert exactly that behaviour rather
than a stronger promise. A deleted author with an intact post is deliberately *not*
treated as a deletion: people delete accounts without deleting their posts.

## Comment trees and the expansion budget

`/comments/<id>` returns a partial tree with `more` placeholders, which
`/api/morechildren` expands — recursively, and without a bound one popular thread costs
thousands of calls (the trap the issue names). Two knobs bound it, per submission:

| knob | default | meaning |
| --- | --- | --- |
| `comment_depth` | `10` | maximum reply nesting kept (also the API's `depth`) |
| `max_more_requests` | `10` | hard cap on `/api/morechildren` calls; `0` disables expansion entirely |
| `comment_limit` | `200` | the API's `limit` for the initial tree |

When a budget bites, the truncation is **never silent**: the document text ends with a
`Comment tree truncated ...` notice, `metadata.comments_truncated` is `true`, and the
connector logs a warning naming the submission and the calls used.

## Rate limiting

Reddit allows roughly 100 requests per minute per OAuth client. The connector reads the
`X-Ratelimit-Remaining` / `X-Ratelimit-Reset` response headers and sleeps out the window
when it is nearly exhausted, rather than hammering it. `429` and `5xx` responses are
retried with backoff, preferring Reddit's own `Retry-After`; timeouts and connection
errors are retried the same way. A `401` triggers exactly one silent re-authentication
(the token expired mid-run). `403`/`404` are permanent answers and propagate — except on
`/comments/<id>`, where they are read as "this thread is not readable any more": the
submission is still yielded, and the delete re-check decides its fate.

## Testing

```bash
uv run --with pytest pytest tests/ -q
```

The tests need no live Reddit and never sleep: the sync core is a pure function over
`(config, state, listings, info)` and the pipeline tests inject a fake OAuth client.
They cover subreddit-name normalization and the env fallbacks, submission rendering
(self post, link post, indented comment tree, truncation notice), the budgeted `more`
expansion (grafting under budget, refusal over budget, depth clipping), the
`before`/`after` cursor including forward-only movement and replay idempotence, the
fingerprint gate (edits and new replies re-emit, vote churn does not), all three
deletion readings, and the dlt `merge` + `_deleted` hard-delete pipeline end to end.
