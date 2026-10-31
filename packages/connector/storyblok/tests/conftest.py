"""In-memory stand-in for the Storyblok Content Delivery API v2, shared by the test modules.

Answers follow Storyblok's documented shapes: ``/v2/cdn/spaces/me`` names the space
and its cache version, and ``/v2/cdn/stories`` pages stories with their nested
``content`` blocks. Publishing, unpublishing and deleting move the cache version,
as on Storyblok; saving a draft does not.
"""

import pytest

from cognee_community_connector_storyblok.storyblok import (
    StoryblokAPIError,
    StoryblokAuthError,
    StoryblokRateLimitedError,
)

SPACE_ID = 287001


def page_content(title, body="", component="page"):
    """A story's content: a block with a heading field and a rich text body."""
    return {
        "_uid": "11111111-2222-3333-4444-555555555555",
        "component": component,
        "_editable": "<!--#storyblok#{...}-->",
        "title": title,
        "body": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": body}]},
            ]
            if body
            else [],
        },
        "image": {
            "id": 9,
            "fieldtype": "asset",
            "filename": "https://a.storyblok.com/f/287001/hero.png",
            "alt": f"{title} hero image",
            "title": "",
        },
        "cta": {"fieldtype": "multilink", "linktype": "url", "url": "https://example.com"},
        "contact": {"fieldtype": "multilink", "linktype": "email", "email": "sales@example.com"},
    }


class FakeStoryblok:
    """Implements ``get`` like Storyblok's CDN. ``page_size`` overrides the page length."""

    def __init__(self, space_id=SPACE_ID, page_size=None):
        self.space_id = space_id
        self.page_size = page_size
        self.published: dict[int, dict] = {}
        self.drafts: dict[int, dict] = {}
        self.cv = 1_700_000_000
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, int] = {}
        self.rate_limited: set[str] = set()
        self.bad_token = False
        self.missing_list = False
        self._next = 0

    def _bump(self):
        self.cv += 1

    def add(self, name, *, slug=None, body="", tags=(), component="page", publish=True):
        self._next += 1
        story_id = 1000 + self._next
        story = {
            "id": story_id,
            "uuid": f"uuid-{story_id}",
            "name": name,
            "slug": slug or name.lower().replace(" ", "-"),
            "full_slug": slug or name.lower().replace(" ", "-"),
            "content": page_content(name, body, component),
            "tag_list": list(tags),
            "lang": "default",
            "created_at": "2026-10-20T10:00:00.000Z",
            "first_published_at": "2026-10-20T10:05:00.000Z" if publish else None,
            "published_at": "2026-10-20T10:05:00.000Z" if publish else None,
        }
        self.drafts[story_id] = story
        if publish:
            self.published[story_id] = dict(story)
            self._bump()
        return story_id

    def edit(self, story_id, body, publish=True):
        """Save a new body; publishing it moves the cache version, a draft save does not."""
        story = dict(self.drafts[story_id])
        story["content"] = page_content(story["name"], body, story["content"]["component"])
        self.drafts[story_id] = story
        if publish:
            story["published_at"] = "2026-10-21T09:00:00.000Z"
            self.published[story_id] = dict(story)
            self._bump()

    def unpublish(self, story_id):
        self.published.pop(story_id)
        self._bump()

    def delete(self, story_id):
        self.published.pop(story_id, None)
        self.drafts.pop(story_id)
        self._bump()

    def _fail(self, path):
        if self.bad_token:
            raise StoryblokAuthError("Storyblok rejected the access token (HTTP 401)")
        if any(key in path for key in self.rate_limited):
            raise StoryblokRateLimitedError("Storyblok rate limit exceeded (HTTP 429)")
        for key, status in self.errors.items():
            if key in path:
                if status == 401:
                    raise StoryblokAuthError("Storyblok rejected the access token (HTTP 401)")
                raise StoryblokAPIError(f"Storyblok request failed: HTTP {status}", status)

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        self._fail(path)
        if path == "/v2/cdn/spaces/me":
            return {"space": {"id": self.space_id, "name": "Docs", "version": self.cv}}
        if path != "/v2/cdn/stories":
            raise AssertionError(f"unexpected path {path}")
        if self.missing_list:
            return {"cv": self.cv, "rels": [], "links": []}
        pool = self.drafts if params.get("version") == "draft" else self.published
        stories = sorted(pool.values(), key=lambda s: s["id"])
        if "starts_with" in params:
            stories = [s for s in stories if s["full_slug"].startswith(params["starts_with"])]
        if "content_type" in params:
            stories = [s for s in stories if s["content"]["component"] == params["content_type"]]
        if "with_tag" in params:
            stories = [s for s in stories if params["with_tag"] in s["tag_list"]]
        size = self.page_size or int(params.get("per_page", 25))
        assert size <= 100, "per_page must be at most 100"
        page = int(params.get("page", 1))
        chunk = stories[(page - 1) * size : page * size]
        return {"stories": [dict(s) for s in chunk], "cv": self.cv, "rels": [], "links": []}

    def paths(self):
        return [p for p, _ in self.calls]


@pytest.fixture
def blok():
    return FakeStoryblok()
