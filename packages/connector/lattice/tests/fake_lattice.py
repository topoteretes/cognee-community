"""An in-memory Lattice Talent API, served through ``httpx.MockTransport``.

It follows the documented behaviour the connector relies on: bearer auth (401),
cursor pagination with ``limit`` capped at 100 and an opaque ``startingAfter``,
the ``{hasMore, endingCursor}`` list shape and the ``{pageInfo}`` shape of the
goal-updates endpoints, the ``state``, ``status`` and ``onlyPublic`` filters,
``{error}`` bodies, 429 with ``Retry-After``, integer timestamps in epoch
seconds, and references that carry only ``id``, ``object`` and ``url``. Tests
drive it like a Lattice admin: add, edit and delete goals and feedback, post
progress updates and answer review questions.
"""

import base64
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

API_KEY = "test-api-key"


class FakeLattice:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.goals: dict[str, dict] = {}
        self.updates: dict[str, list[dict]] = {}
        self.feedbacks: dict[str, dict] = {}
        self.cycles: dict[str, dict] = {}
        self.reviewees: dict[str, dict] = {}
        self.questions: dict[str, dict] = {}
        self.reviews: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []  # (method, path, params)
        self.hosts: list[str] = []
        # Called before each request, to change data while a listing is read.
        self.on_request: Callable[[str, dict], None] | None = None
        # path prefix -> responses to give before answering normally.
        self._failures: dict[str, list[httpx.Response]] = {}
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)
        self._counter = 0

    # -- admin actions -------------------------------------------------------
    def add_user(self, name: str, status: str = "ACTIVE", email: str | None = None) -> str:
        user_id = self._id()
        self.users[user_id] = {
            "id": user_id,
            "object": "user",
            "url": f"https://lattice.test/users/{user_id}",
            "name": name,
            "preferredName": None,
            "email": email or f"{name.lower().replace(' ', '.')}@acme.test",
            "status": status,
            "isAdmin": False,
            "createdAt": self._now(),
            "updatedAt": self._now(),
        }
        return user_id

    def add_goal(
        self,
        name: str,
        owners: list[str],
        *,
        state: str = "Active",
        private: bool = False,
        parent: str | None = None,
        **fields,
    ) -> str:
        goal_id = self._id()
        now = self._now()
        self.goals[goal_id] = {
            "id": goal_id,
            "object": "goal",
            "url": f"https://lattice.test/goals/{goal_id}",
            "owners": self._list([self._ref("user", u) for u in owners]),
            "childGoals": self._list([]),
            "tags": self._list([]),
            "parentGoal": self._ref("goal", parent) if parent else None,
            "name": name,
            "description": fields.pop("description", ""),
            "state": state,
            "status": "NotUpdated",
            "goalType": "Individual",
            "okrType": "objective",
            "isPrivate": private,
            "dueDate": "2026-03-31",
            "startDate": "2026-01-01",
            "createdAt": now,
            "updatedAt": now,
            **fields,
        }
        self.updates[goal_id] = []
        return goal_id

    def edit_goal(self, goal_id: str, **fields) -> None:
        self.goals[goal_id].update(fields, updatedAt=self._now())

    def touch_goal(self, goal_id: str) -> None:
        """Bump ``updatedAt`` without changing any field (a no-op save)."""
        self.goals[goal_id]["updatedAt"] = self._now()

    def add_goal_update(self, goal_id: str, comment: str, status="green", increment=None) -> None:
        """A check-in. Per Lattice, it moves ``lastUpdatedAt`` but not ``updatedAt``."""
        entries = self.updates[goal_id]
        entries.append(
            {
                "entityId": f"00000000-0000-4000-8000-{self._counter:012d}",
                "goalId": len(self.goals),
                "id": len(entries) + 1,
                "comment": comment,
                "status": status,
                "increment": increment,
            }
        )
        self._counter += 1
        self.goals[goal_id]["lastUpdatedAt"] = self._now()

    def delete_goal(self, goal_id: str) -> None:
        del self.goals[goal_id]
        del self.updates[goal_id]

    def add_feedback(
        self, author: str, targets: list[str], body: str, *, public: bool = True
    ) -> str:
        feedback_id = self._id()
        self.feedbacks[feedback_id] = {
            "id": feedback_id,
            "object": "feedback",
            "url": f"https://lattice.test/feedback/{feedback_id}",
            "author": self._ref("user", author),
            "targets": self._list([self._ref("user", u) for u in targets]),
            "associatedValues": self._list([]),
            "feedbackRequest": None,
            "body": body,
            "visibility": "Company" if public else "Private",
            "isPublic": public,
            "createdAt": self._now(),
        }
        return feedback_id

    def delete_feedback(self, feedback_id: str) -> None:
        del self.feedbacks[feedback_id]

    def add_cycle(self, name: str, stage: str = "CollectingReviews") -> str:
        cycle_id = self._id()
        self.cycles[cycle_id] = {
            "id": cycle_id,
            "object": "reviewCycle",
            "url": f"https://lattice.test/reviewCycles/{cycle_id}",
            "name": name,
            "stage": stage,
            "reviewsStartedAt": self._now(),
            "calibrationEnabled": False,
            "createdAt": self._now(),
            "updatedAt": self._now(),
        }
        return cycle_id

    def add_reviewee(self, cycle_id: str, user_id: str) -> str:
        reviewee_id = self._id()
        self.reviewees[reviewee_id] = {
            "id": reviewee_id,
            "object": "reviewee",
            "url": f"https://lattice.test/reviewees/{reviewee_id}",
            "cycle": cycle_id,
            "reviewCycle": self._ref("reviewCycle", cycle_id),
            "user": self._ref("user", user_id),
            "createdAt": self._now(),
            "updatedAt": self._now(),
        }
        return reviewee_id

    def add_question(self, body: str) -> str:
        revision_id = self._id()
        self.questions[revision_id] = {
            "id": revision_id,
            "object": "questionRevision",
            "url": f"https://lattice.test/questionRevisions/{revision_id}",
            "body": body,
        }
        return revision_id

    def add_review(
        self,
        cycle_id: str,
        reviewee_id: str,
        question_id: str,
        reviewer_id: str,
        review_type: str = "Peer",
        *,
        comment: str | None = None,
        rating: int | None = None,
        submitted: bool = True,
        declined: bool = False,
        visible: bool = True,
        index: int = 0,
    ) -> str:
        review_id = self._id()
        response = {"comment": comment, "createdAt": self._now(), "updatedAt": self._now()}
        if rating is not None:
            response.update(rating=rating, ratingString=str(rating), preciseRating=float(rating))
        self.reviews[review_id] = {
            "id": review_id,
            "object": "review",
            "url": f"https://lattice.test/reviews/{review_id}",
            "cycle": cycle_id,
            "reviewee": self._ref("reviewee", reviewee_id),
            "reviewer": self._ref("user", reviewer_id),
            "question": self._ref("question", question_id),
            "questionRevision": self._ref("questionRevision", question_id),
            "reviewType": review_type,
            # Null when unwritten or when the key's owner may not read it.
            "response": response if visible else None,
            "calibratedResponse": None,
            "submittedAt": self._now() if submitted else None,
            "declinedAt": self._now() if declined else None,
            "questionIndex": index,
        }
        return review_id

    def edit_review(self, review_id: str, **response) -> None:
        self.reviews[review_id]["response"].update(response, updatedAt=self._now())

    def delete_review(self, review_id: str) -> None:
        del self.reviews[review_id]

    def delete_cycle(self, cycle_id: str) -> None:
        del self.cycles[cycle_id]
        self.reviews = {k: v for k, v in self.reviews.items() if v["cycle"] != cycle_id}
        self.reviewees = {k: v for k, v in self.reviewees.items() if v["cycle"] != cycle_id}

    # -- failure injection ---------------------------------------------------
    def fail(self, path_prefix: str, status: int, *, times: int = 1, headers=None) -> None:
        response = httpx.Response(status, headers=headers or {}, json={"error": "injected"})
        self._failures.setdefault(path_prefix, []).extend([response] * times)

    # -- transport -----------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def requests_to(self, fragment: str) -> list[tuple[str, str, dict]]:
        return [call for call in self.calls if fragment in call[1]]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append((request.method, path, params))
        self.hosts.append(request.url.host)
        if self.on_request:
            self.on_request(path, params)
        if request.headers.get("authorization") != f"Bearer {API_KEY}":
            return _error(401, "unauthorized")
        for prefix, queue in self._failures.items():
            if path.startswith(prefix) and queue:
                return queue.pop(0)
        if request.method != "GET":
            return _error(405, "method not allowed")
        return self._route(path, params)

    def _route(self, path: str, params: dict) -> httpx.Response:
        parts = path.strip("/").split("/")
        if parts[0] != "v1":
            return _error(404, "not found")
        route = parts[1:]
        if route == ["users"]:
            users = list(self.users.values())
            if params.get("status") != "NULL":
                users = [u for u in users if u["status"] == params.get("status", "ACTIVE")]
            return self._page(users, params)
        if route == ["goals"]:
            goals = list(self.goals.values())
            if "state" in params:
                goals = [g for g in goals if g["state"] == params["state"]]
            return self._page(goals, params)
        if len(route) == 3 and route[0] == "goals" and route[2] == "updates":
            if route[1] not in self.updates:
                return _error(404, "goal not found")
            return self._page(self.updates[route[1]], params, shape="pageInfo")
        if route == ["feedbacks"]:
            items = list(self.feedbacks.values())
            if params.get("onlyPublic") == "true":
                items = [f for f in items if f["isPublic"]]
            return self._page(items, params)
        if route == ["reviewCycles"]:
            return self._page(list(self.cycles.values()), params)
        if len(route) == 3 and route[0] == "reviewCycle" and route[2] == "reviewees":
            return self._page(
                [r for r in self.reviewees.values() if r["cycle"] == route[1]], params
            )
        if len(route) == 3 and route[0] == "reviewCycle" and route[2] == "reviews":
            return self._page([r for r in self.reviews.values() if r["cycle"] == route[1]], params)
        if len(route) == 2 and route[0] == "questionRevision":
            question = self.questions.get(route[1])
            return httpx.Response(200, json=question) if question else _error(404, "not found")
        return _error(404, "not found")

    def _page(self, rows: list[dict], params: dict, *, shape: str = "list") -> httpx.Response:
        limit = int(params.get("limit", 10))
        if not 1 <= limit <= 100:
            return _error(400, "limit must be between 1 and 100")
        rows = list(reversed(rows))  # Lattice lists newest first
        start = _decode(params["startingAfter"]) if "startingAfter" in params else 0
        chunk = rows[start : start + limit]
        more = start + limit < len(rows)
        cursor = _encode(start + limit) if chunk else None
        if shape == "pageInfo":
            body = {
                "data": chunk,
                "pageInfo": {
                    "startCursor": _encode(start) if chunk else None,
                    "endCursor": cursor,
                    "hasNextPage": more,
                },
            }
        else:
            body = {
                "object": "list",
                "hasMore": more,
                "endingCursor": cursor if more else None,
                "data": chunk,
            }
        return httpx.Response(
            200,
            json=body,
            headers={"X-RateLimit-Limit": "240", "X-RateLimit-Remaining": "239"},
        )

    # -- helpers -------------------------------------------------------------
    def _id(self) -> str:
        self._counter += 1
        return f"{self._counter:08x}-0000-4000-8000-{self._counter:012x}"

    def _now(self) -> int:
        self._clock += timedelta(minutes=1)
        return int(self._clock.timestamp())

    @staticmethod
    def _ref(kind: str, ref_id: str) -> dict:
        return {"id": ref_id, "object": kind, "url": f"https://lattice.test/{kind}/{ref_id}"}

    @staticmethod
    def _list(data: list[dict]) -> dict:
        return {"object": "list", "hasMore": False, "endingCursor": None, "data": data}


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": message})


def _encode(offset: int) -> str:
    return base64.b64encode(f"cursor:{offset}".encode()).decode()


def _decode(cursor: str) -> int:
    return int(base64.b64decode(cursor).decode().split(":")[1])
