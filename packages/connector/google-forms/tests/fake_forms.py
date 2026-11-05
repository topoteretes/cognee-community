"""In-memory Google Forms API test double."""

from typing import Any


class HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"<HttpError {status}>")
        self.resp = type("Resp", (), {"status": status})()


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class _Responses:
    def __init__(self, fake):
        self.fake = fake

    def list(self, formId: str, filter: str = None, pageToken: str = None):
        return _Request(lambda: self.fake._list_responses(formId, filter))


class _FormsResource:
    def __init__(self, fake):
        self.fake = fake

    def get(self, formId: str):
        return _Request(lambda: self.fake._get_form(formId))

    def responses(self):
        return _Responses(self.fake)


class FakeFormsService:
    def __init__(self):
        self._forms: dict[str, dict[str, Any]] = {}
        self._responses: dict[str, list[dict[str, Any]]] = {}

    def forms(self):
        return _FormsResource(self)

    def add_form(self, form_id: str, title: str, description: str, questions: list[dict]):
        items = []
        for idx, q in enumerate(questions, 1):
            q_id = q.get("id", f"q{idx}")
            items.append({
                "itemId": q_id,
                "title": q.get("title", f"Question {idx}"),
                "questionItem": {
                    "question": {
                        "questionId": q_id,
                        q.get("type", "textQuestion"): q.get("type_data", {}),
                    }
                }
            })

        self._forms[form_id] = {
            "formId": form_id,
            "info": {
                "title": title,
                "description": description,
            },
            "items": items,
        }
        if form_id not in self._responses:
            self._responses[form_id] = []

    def add_response(
        self,
        form_id: str,
        response_id: str,
        answers: dict[str, list[str]],
        timestamp: str = "2026-11-04T12:00:00Z",
        respondent_email: str = None,
    ):
        formatted_answers = {}
        for q_id, values in answers.items():
            formatted_answers[q_id] = {
                "questionId": q_id,
                "textAnswers": {
                    "answers": [{"value": v} for v in values]
                }
            }

        resp_obj = {
            "responseId": response_id,
            "createTime": timestamp,
            "lastSubmittedTime": timestamp,
            "answers": formatted_answers,
        }
        if respondent_email:
            resp_obj["respondentEmail"] = respondent_email

        self._responses.setdefault(form_id, []).append(resp_obj)

    def delete_form(self, form_id: str):
        self._forms.pop(form_id, None)

    def _get_form(self, form_id: str) -> dict[str, Any]:
        if form_id not in self._forms:
            raise HttpError(404)
        return self._forms[form_id]

    def _list_responses(self, form_id: str, filter_query: str = None) -> dict[str, Any]:
        if form_id not in self._forms:
            raise HttpError(404)

        all_resps = self._responses.get(form_id, [])
        if not filter_query:
            return {"responses": all_resps}

        # Filter format: 'timestamp >= YYYY-MM-DD...'
        prefix = "timestamp >= "
        if filter_query.startswith(prefix):
            threshold = filter_query[len(prefix):]
            filtered = [r for r in all_resps if (r.get("lastSubmittedTime") or "") >= threshold]
            return {"responses": filtered}

        return {"responses": all_resps}
