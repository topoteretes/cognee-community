# Recorded Reddit API responses

These files are real Reddit API responses, trimmed, taken from the integration test
cassettes of [PRAW](https://github.com/praw-dev/praw) (`tests/integration/cassettes`).
Each file names the cassette it comes from (`source`) and the request it answers
(`request`). The fake API in `tests/conftest.py` builds its answers from them, so the
tests run against the shapes Reddit actually sends.

| File | What it shows |
| --- | --- |
| `token.json` | The token answer of `POST /api/v1/access_token`. |
| `comments.json` | `GET /comments/{id}`: two listings, the post first and the comments second. |
| `morechildren.json` | `POST /api/morechildren`: a flat list of comments with their `parent_id`. |
| `continue_thread.json` | A "continue this thread" `more` node (`id` `_`, no children). |
| `info.json` | `GET /api/info`: posts by fullname, one removed (`selftext` `[removed]`). |
| `forbidden.json` | The 403 of a quarantined subreddit's listing, which carries no `reason`. |

They are used under PRAW's license:

```
Copyright (c) 2016, Bryce Boe
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
