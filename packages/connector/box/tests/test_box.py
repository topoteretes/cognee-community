"""Box document sync contracts, without a provider account."""

import copy

import pytest

from cognee_community_connector_box.box import BoxSyncError, sync_box


class FakeBox:
    def __init__(self):
        self.calls = []
        self.folder_name = "Engineering"
        self.files = {
            "10": {
                "id": "10",
                "type": "file",
                "name": "notes.txt",
                "size": 5,
                "etag": "1",
                "item_status": "active",
                "parent": {"id": "1"},
                "path_collection": {"entries": [{"id": "0"}, {"id": "1"}]},
                "file_version": {"id": "v1"},
                "description": "",
            }
        }
        self.text = {"10": "Alpha"}
        self.comments = {"10": [{"id": "20", "message": "Review Alpha", "type": "comment"}]}
        self.events = {}
        self.fail_listing = False
        self.missing_status = 404

    def changed(self, event_type="ITEM_MODIFY", kind="file", identifier="10"):
        self.events["p0"] = {
            "entries": [
                {
                    "event_id": "e1",
                    "event_type": event_type,
                    "source": {"type": kind, "id": identifier},
                }
            ],
            "next_stream_position": "p1",
        }

    def get_json(self, path, params=None, headers=None):
        self.calls.append((path, copy.deepcopy(params)))
        if path == "users/me":
            return {"id": "100"}
        if path == "events":
            position = params["stream_position"]
            if position == "now":
                return {"entries": [], "next_stream_position": "p0"}
            return self.events.get(position, {"entries": [], "next_stream_position": position})
        if path == "folders/1":
            return {
                "id": "1",
                "type": "folder",
                "name": self.folder_name,
                "item_status": "active",
                "description": "Team documents",
            }
        if path == "folders/1/items":
            if self.fail_listing:
                raise BoxSyncError("Incomplete folder listing")
            return {
                "entries": [
                    x
                    for x in self.files.values()
                    if x["item_status"] == "active" and x["parent"]["id"] == "1"
                ],
                "next_marker": None,
            }
        if path.startswith("files/") and path.endswith("/comments"):
            fid = path.split("/")[1]
            rows = self.comments.get(fid, [])
            return {"entries": rows, "total_count": len(rows), "offset": 0, "limit": 1000}
        if path.startswith("files/"):
            fid = path.split("/")[1]
            if fid in self.files:
                return copy.deepcopy(self.files[fid])
            raise BoxSyncError("Object is inaccessible", status=self.missing_status)
        if path.startswith("comments/"):
            identifier = path.split("/")[1]
            for file_id, comments in self.comments.items():
                for comment in comments:
                    if comment["id"] == identifier:
                        return {**comment, "item": {"id": file_id, "type": "file"}}
            raise BoxSyncError("Comment is inaccessible", status=self.missing_status)
        raise AssertionError(path)

    def get_text(self, path, max_bytes, params=None):
        self.calls.append((path, params))
        return self.text[path.split("/")[1]]


def test_baseline_has_folder_file_and_comment_and_captures_cursor_first():
    client, state = FakeBox(), {}
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert {r["id"] for r in rows} == {"box:100:folder:1", "box:100:file:10", "box:100:comment:20"}
    assert "Alpha" in next(r["content"] for r in rows if r["id"].endswith("file:10"))
    assert client.calls[1] == ("events", {"stream_position": "now", "stream_type": "all"})
    assert state["stream_position"] == "p0"


def test_unchanged_run_downloads_no_file_text_and_emits_no_rows():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.calls.clear()
    assert list(sync_box(client, state, folder_ids=["1"])) == []
    assert not any(path.endswith("/content") for path, _ in client.calls)


def test_comment_only_edit_is_synced_without_file_download():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.comments["10"][0]["message"] = "Review Bravo"
    client.changed("COMMENT_EDIT")
    client.calls.clear()
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert len(rows) == 1 and rows[0]["id"].endswith("comment:20")
    assert "Bravo" in rows[0]["content"]
    assert not any(path.endswith("/content") for path, _ in client.calls)


def test_trash_file_retires_owned_file_and_comment():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.files["10"]["item_status"] = "trashed"
    client.changed("DELETE")
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert {r["id"] for r in rows} == {"box:100:file:10", "box:100:comment:20"}
    assert all(r["_deleted"] for r in rows)


def test_failed_listing_preserves_cursor_and_inventory_and_emits_nothing():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    original = copy.deepcopy(state)
    client.fail_listing = True
    client.changed(kind="folder", identifier="1")
    iterator = sync_box(client, state, folder_ids=["1"])
    with pytest.raises(BoxSyncError):
        next(iterator)
    assert state == original


def test_ambiguous_missing_file_does_not_delete_or_acknowledge():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    original = copy.deepcopy(state)
    client.files.clear()
    client.changed()
    with pytest.raises(BoxSyncError):
        list(sync_box(client, state, folder_ids=["1"]))
    assert state == original


def test_short_event_page_follows_next_position():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.events["p0"] = {
        "entries": [
            {"event_id": "e1", "event_type": "ITEM_MODIFY", "source": {"type": "file", "id": "10"}}
        ],
        "next_stream_position": "p1",
    }
    client.events["p1"] = {"entries": [], "next_stream_position": "p2"}
    list(sync_box(client, state, folder_ids=["1"]))
    assert any(
        path == "events" and params["stream_position"] == "p1" for path, params in client.calls
    )
    assert state["stream_position"] == "p2"


def test_unchanged_run_only_polls_events_and_selected_comments():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.calls.clear()
    assert list(sync_box(client, state, folder_ids=["1"])) == []
    assert {path for path, _ in client.calls} == {"users/me", "events", "files/10/comments"}


def test_comment_edit_without_event_is_detected_without_file_download():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.calls.clear()
    client.comments["10"][0]["message"] = "Review Bravo"
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert len(rows) == 1 and rows[0]["id"] == "box:100:comment:20"
    assert rows[0]["content"] == "Review Bravo"
    assert not any(path == "files/10" or path.endswith("/content") for path, _ in client.calls)


def test_comments_are_not_polled_when_not_selected():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"], resources=("files",)))
    client.calls.clear()
    assert list(sync_box(client, state, folder_ids=["1"], resources=("files",))) == []
    assert {path for path, _ in client.calls} == {"users/me", "events"}


def test_failed_comment_poll_keeps_original_documents_and_cursor():
    class FailedComments(FakeBox):
        fail_comments = False

        def get_json(self, path, params=None, headers=None):
            if self.fail_comments and path.endswith("/comments"):
                raise BoxSyncError("Comments unavailable", status=403)
            return super().get_json(path, params=params, headers=headers)

    client, state = FailedComments(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    original = copy.deepcopy(state)
    client.fail_comments = True
    with pytest.raises(BoxSyncError, match="Comments unavailable"):
        list(sync_box(client, state, folder_ids=["1"]))
    assert state == original


def test_confirmed_delete_event_allows_404_but_403_does_not():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.files.clear()
    client.changed("DELETE")
    client.missing_status = 403
    original = copy.deepcopy(state)
    with pytest.raises(BoxSyncError):
        list(sync_box(client, state, folder_ids=["1"]))
    assert state == original
    client.missing_status = 404
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert len(rows) == 2 and all(row["_deleted"] for row in rows)


def test_replayed_event_does_not_emit_unchanged_documents():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.changed()
    assert list(sync_box(client, state, folder_ids=["1"])) == []


def test_scope_change_is_rejected_without_discarding_existing_ownership():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    with pytest.raises(ValueError, match="scope"):
        list(sync_box(client, state, folder_ids=["1"], resources=("files",)))


def test_comment_offsets_advance_by_response_limit_not_entry_count():
    from cognee_community_connector_box.box import _comments

    class SparseComments:
        def get_json(self, path, params):
            assert path == "files/10/comments"
            offset = params["offset"]
            assert offset in (0, 1000)
            return {
                "offset": offset,
                "limit": 1000,
                "total_count": 1001,
                "entries": [{"id": str(offset), "message": "Sparse comment"}],
            }

    assert [row["id"] for row in _comments(SparseComments(), "10")] == ["0", "1000"]


@pytest.mark.parametrize("terminal", [{}, {"next_marker": None}, {"next_marker": ""}])
def test_terminal_folder_page_accepts_omitted_null_or_empty_marker(terminal):
    from cognee_community_connector_box.box import _marker_items

    class TerminalFolder:
        def get_json(self, path, params):
            return {"entries": [{"id": "10", "type": "file"}], "limit": 1000, **terminal}

    assert list(_marker_items(TerminalFolder(), "1")) == [{"id": "10", "type": "file"}]


@pytest.mark.parametrize("event_types", [("DELETE", "ITEM_MODIFY"), ("ITEM_MODIFY", "DELETE")])
def test_out_of_order_modify_does_not_erase_confirmed_delete(event_types):
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.files.clear()
    client.events["p0"] = {
        "entries": [
            {"event_type": event_type, "source": {"type": "file", "id": "10"}}
            for event_type in event_types
        ],
        "next_stream_position": "p1",
    }
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {"box:100:file:10", "box:100:comment:20"}
    assert all(row["_deleted"] for row in rows)
    assert state["stream_position"] == "p1"


def test_sdk_oauth_refresh_persists_rotated_tokens(tmp_path, monkeypatch):
    import json

    import requests

    from cognee_community_connector_box.box import _sdk_client

    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"access_token": "expired-test", "refresh_token": "old-test"}))
    monkeypatch.setenv("BOX_CLIENT_ID", "test-client")
    monkeypatch.setenv("BOX_CLIENT_SECRET", "test-secret")
    monkeypatch.delenv("BOX_JWT_CONFIG_PATH", raising=False)
    calls = []

    def request(session, method, url, **kwargs):
        assert method == "POST" and url == "https://api.box.com/oauth2/token"
        assert kwargs["data"]["grant_type"] == "refresh_token"
        assert kwargs["data"]["refresh_token"] == "old-test"
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "access_token": "new-test",
                "refresh_token": "rotated-test",
                "expires_in": 3600,
                "token_type": "bearer",
            }
        ).encode()
        response.request = requests.Request(method, url).prepare()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    client = _sdk_client(token_path=path)
    assert client.auth.refresh("expired-test") == ("new-test", "rotated-test")
    assert json.loads(path.read_text()) == {
        "access_token": "new-test",
        "refresh_token": "rotated-test",
    }
    assert len(calls) == 1 and not path.with_suffix(".json.tmp").exists()


def test_jwt_config_signs_enterprise_assertion_before_requesting_token(tmp_path, monkeypatch):
    import json

    import jwt
    import requests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from cognee_community_connector_box.box import _sdk_client

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"fixture-passphrase"),
    )
    config = tmp_path / "jwt.json"
    config.write_text(
        json.dumps(
            {
                "boxAppSettings": {
                    "clientID": "fixture-client",
                    "clientSecret": "fixture-secret",
                    "appAuth": {
                        "publicKeyID": "fixture-key",
                        "privateKey": private_pem.decode(),
                        "passphrase": "fixture-passphrase",
                    },
                },
                "enterpriseID": "fixture-enterprise",
            }
        )
    )
    calls = []

    def request(session, method, url, **kwargs):
        assert method == "POST" and url == "https://api.box.com/oauth2/token"
        data = kwargs["data"]
        assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assertion = data["assertion"]
        claims = jwt.decode(
            assertion,
            key.public_key(),
            algorithms=["RS256"],
            audience="https://api.box.com/oauth2/token",
        )
        assert claims["iss"] == "fixture-client"
        assert claims["sub"] == "fixture-enterprise"
        assert claims["box_sub_type"] == "enterprise"
        assert jwt.get_unverified_header(assertion)["kid"] == "fixture-key"
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "access_token": "fixture-access",
                "expires_in": 3600,
                "token_type": "bearer",
            }
        ).encode()
        response.request = requests.Request(method, url).prepare()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    client = _sdk_client(jwt_config_path=str(config))
    assert client.auth.authenticate_instance() == "fixture-access"
    assert len(calls) == 1


def test_sdk_download_accepts_text_response_and_enforces_byte_limit(monkeypatch):
    import io

    import requests
    from boxsdk import Client, OAuth2

    from cognee_community_connector_box.box import BoxAPI

    def request(session, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/plain"
        response.raw = io.BytesIO(b"Alpha")
        response.request = requests.Request(method, url).prepare()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    api = BoxAPI(Client(OAuth2("test-id", "test-secret", access_token="test-access")))
    assert api.get_text("files/10/content", 5) == "Alpha"
    with pytest.raises(BoxSyncError, match="max_content_bytes"):
        api.get_text("files/10/content", 4)


@pytest.mark.parametrize("received_state", ["expected-state", "wrong-state"])
def test_oauth_callback_checks_state_before_exchanging_code(tmp_path, monkeypatch, received_state):
    import io
    import runpy
    from pathlib import Path

    main = runpy.run_path(str(Path(__file__).parents[1] / "examples" / "authorize.py"))["main"]
    monkeypatch.setenv("BOX_CLIENT_ID", "fake-id")
    monkeypatch.setenv("BOX_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("BOX_TOKEN_PATH", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("BOX_REDIRECT_URI", "http://localhost:8765/callback")
    codes = []

    class FakeOAuth:
        def __init__(self, *args, **kwargs):
            pass

        def get_authorization_url(self, redirect):
            return "https://account.box.com/authorize", "expected-state"

        def authenticate(self, code):
            codes.append(code)

    class FakeServer:
        def __init__(self, address, handler):
            self.handler = handler

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self):
            request = self.handler.__new__(self.handler)
            request.path = f"/callback?code=fake-code&state={received_state}"
            request.send_response = lambda status: None
            request.end_headers = lambda: None
            request.wfile = io.BytesIO()
            request.do_GET()

    monkeypatch.setitem(main.__globals__, "OAuth2", FakeOAuth)
    monkeypatch.setitem(main.__globals__, "HTTPServer", FakeServer)
    monkeypatch.setattr(main.__globals__["webbrowser"], "open", lambda url: True)
    if received_state == "expected-state":
        main()
        assert codes == ["fake-code"]
    else:
        with pytest.raises(RuntimeError, match="No valid OAuth callback"):
            main()
        assert codes == []


def test_last_document_hard_delete_empties_real_staging_table(tmp_path):
    import dlt

    from cognee_community_connector_box import box_source

    client = FakeBox()
    pipeline = dlt.pipeline(
        pipeline_name="box_last",
        dataset_name="box_last",
        pipelines_dir=str(tmp_path / "state"),
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'staging.db').as_posix()}"
        ),
    )
    for deleted in (False, True):
        if deleted:
            client.files.clear()
            client.changed("ITEM_TRASH")
        src = box_source(client=client, folder_ids=["1"], resources=("files",))
        pipeline.run(src)
        table = next(iter(src.resources))
        with pipeline.sql_client() as sql, sql.execute_query(f'SELECT id FROM "{table}"') as cursor:
            assert len(cursor.fetchall()) == (0 if deleted else 1)


def test_real_dlt_merge_retains_unchanged_rows_and_hard_deletes_file_and_comments(tmp_path):
    import dlt

    from cognee_community_connector_box import box_source

    client = FakeBox()
    pipeline = dlt.pipeline(
        pipeline_name="box_test",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'staging.db').as_posix()}"
        ),
        dataset_name="box_test",
        pipelines_dir=str(tmp_path / "state"),
    )

    def run():
        src = box_source(client=client, folder_ids=["1"])
        pipeline.run(src)
        table = next(iter(src.resources))
        with pipeline.sql_client() as sql, sql.execute_query(f'SELECT id FROM "{table}"') as cursor:
            return {row[0] for row in cursor.fetchall()}

    assert len(run()) == 3
    assert len(run()) == 3
    client.files.clear()
    client.changed("DELETE")
    assert run() == {"box:100:folder:1"}


class TreeBox(FakeBox):
    """Current provider metadata for moves independently of event ordering."""

    def __init__(self):
        super().__init__()
        self.parents = {"1": "0", "3": "1", "2": "3", "10": "2"}
        self.statuses = {}
        self.fail_items = {}

    def get_json(self, path, params=None, headers=None):
        parts = path.split("/")
        if parts[0] in {"folders", "files"} and len(parts) == 2:
            identifier = parts[1]
            if identifier in self.fail_items:
                raise BoxSyncError("Descendant unavailable", status=self.fail_items[identifier])
            kind = "file" if identifier == "10" else "folder"
            parent = self.parents[identifier]
            ancestry = [parent]
            while ancestry[0] != "0":
                ancestry.insert(0, self.parents.get(ancestry[0], "0"))
            return {
                **(self.files["10"] if kind == "file" else {}),
                "id": identifier,
                "type": kind,
                "name": "notes.txt" if kind == "file" else f"Folder {identifier}",
                "item_status": self.statuses.get(identifier, "active"),
                "parent": {"id": parent},
                "path_collection": {"entries": [{"id": x} for x in ancestry]},
            }
        if parts[0] == "folders" and parts[-1] == "items":
            return {
                "entries": [
                    {"type": "file" if x == "10" else "folder", "id": x}
                    for x, parent in self.parents.items()
                    if parent == parts[1] and self.statuses.get(x, "active") == "active"
                ],
                "next_marker": None,
            }
        return super().get_json(path, params=params, headers=headers)

    def events_for(self, events):
        self.events["p0"] = {
            "entries": [
                {"event_type": event_type, "source": {"type": kind, "id": identifier}}
                for event_type, kind, identifier in events
            ],
            "next_stream_position": "p1",
        }


def test_selected_nested_root_survives_ancestor_scope_exit():
    client, state = TreeBox(), {}
    list(sync_box(client, state, folder_ids=["1", "2"]))
    client.parents["3"] = "9"
    client.events_for([("ITEM_MOVE", "folder", "3")])
    rows = list(sync_box(client, state, folder_ids=["1", "2"]))
    assert rows == [{"id": "box:100:folder:3", "_deleted": True}]
    assert set(state["documents"]) == {
        "box:100:folder:1",
        "box:100:folder:2",
        "box:100:file:10",
        "box:100:comment:20",
    }
    assert state["objects"]["file:10"]["path_collection"]["entries"] == [
        {"id": "0"},
        {"id": "9"},
        {"id": "3"},
        {"id": "2"},
    ]


@pytest.mark.parametrize(
    "events",
    [
        [("DELETE", "folder", "3")],
        [("DELETE", "folder", "3"), ("ITEM_MOVE", "file", "10")],
        [("ITEM_MOVE", "file", "10"), ("DELETE", "folder", "3")],
    ],
)
def test_current_move_to_selected_root_survives_old_parent_delete(events):
    client, state = TreeBox(), {}
    client.parents.update({"2": "0", "10": "3"})
    list(sync_box(client, state, folder_ids=["1", "2"]))
    client.parents["10"] = "2"
    client.statuses["3"] = "trashed"
    client.events_for(events)
    rows = list(sync_box(client, state, folder_ids=["1", "2"]))
    assert rows == [{"id": "box:100:folder:3", "_deleted": True}]
    assert "box:100:file:10" in state["documents"]
    assert "box:100:comment:20" in state["documents"]
    assert state["stream_position"] == "p1"


@pytest.mark.parametrize("status", [403, 404])
def test_ambiguous_descendant_read_aborts_parent_retirement(status):
    client, state = TreeBox(), {}
    list(sync_box(client, state, folder_ids=["1", "2"]))
    original = copy.deepcopy(state)
    client.parents["3"] = "9"
    client.fail_items["10"] = status
    client.events_for([("ITEM_MOVE", "folder", "3")])
    with pytest.raises(BoxSyncError, match="Descendant unavailable"):
        next(sync_box(client, state, folder_ids=["1", "2"]))
    assert state == original


def test_folder_scope_exit_retires_unselected_current_descendants():
    client, state = TreeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.parents["3"] = "9"
    client.events_for([("ITEM_MOVE", "folder", "3")])
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert {row["id"] for row in rows} == {
        "box:100:folder:3",
        "box:100:folder:2",
        "box:100:file:10",
        "box:100:comment:20",
    }
    assert all(row["_deleted"] for row in rows)
    assert set(state["documents"]) == {"box:100:folder:1"}


@pytest.mark.parametrize(
    "pages",
    [
        [
            {"entries": [{"id": "20"}], "total_count": 2, "offset": 0, "limit": 1},
            {"entries": [], "total_count": 1, "offset": 1, "limit": 1},
        ],
        [
            {"entries": [{"id": "20"}], "total_count": 2, "offset": 0, "limit": 1},
            {"entries": [{"id": "20"}], "total_count": 2, "offset": 1, "limit": 1},
        ],
    ],
)
def test_incomplete_comment_pages_preserve_corpus_and_cursor(pages):
    class IncompleteComments(FakeBox):
        partial = False

        def get_json(self, path, params=None, headers=None):
            if self.partial and path.endswith("/comments"):
                return pages[params["offset"]]
            return super().get_json(path, params=params, headers=headers)

    client, state = IncompleteComments(), {}
    client.comments["10"].append({"id": "21", "message": "Keep second comment"})
    list(sync_box(client, state, folder_ids=["1"]))
    original = copy.deepcopy(state)
    client.partial = True
    with pytest.raises(BoxSyncError):
        next(sync_box(client, state, folder_ids=["1"]))
    assert state == original


def test_complete_comment_pages_advance_by_response_limit():
    from cognee_community_connector_box.box import _comments

    class CompleteComments:
        def get_json(self, path, params):
            offset = params["offset"]
            return {
                "offset": offset,
                "limit": 2,
                "total_count": 3,
                "entries": [{"id": str(x)} for x in ({0: [20, 21], 2: [22]}[offset])],
            }

    assert [row["id"] for row in _comments(CompleteComments(), "10")] == ["20", "21", "22"]


def test_folder_trash_retires_confirmed_trashed_descendants():
    client, state = TreeBox(), {}
    list(sync_box(client, state, folder_ids=["1", "2"]))
    client.statuses.update({"3": "trashed", "2": "trashed", "10": "trashed"})
    client.events_for([("ITEM_TRASH", "folder", "3")])
    rows = list(sync_box(client, state, folder_ids=["1", "2"]))
    assert {row["id"] for row in rows} == {
        "box:100:folder:3",
        "box:100:folder:2",
        "box:100:file:10",
        "box:100:comment:20",
    }
    assert all(row["_deleted"] for row in rows)
    assert set(state["documents"]) == {"box:100:folder:1"}


@pytest.mark.parametrize("child_first", [False, True])
def test_missing_descendant_with_own_delete_event_can_be_retired(child_first):
    client, state = TreeBox(), {}
    client.parents.update({"2": "0", "10": "3"})
    list(sync_box(client, state, folder_ids=["1", "2"]))
    client.statuses["3"] = "trashed"
    client.fail_items["10"] = 404
    events = [("DELETE", "folder", "3"), ("DELETE", "file", "10")]
    client.events_for(events[::-1] if child_first else events)
    rows = list(sync_box(client, state, folder_ids=["1", "2"]))
    assert {row["id"] for row in rows} == {
        "box:100:folder:3",
        "box:100:file:10",
        "box:100:comment:20",
    }
    assert all(row["_deleted"] for row in rows)
    assert state["stream_position"] == "p1"


def test_missing_comment_from_sparse_listing_is_rechecked_before_deletion():
    class SparseListing(FakeBox):
        partial = False

        def get_json(self, path, params=None, headers=None):
            if self.partial and path.endswith("/comments"):
                return {
                    "entries": [{"id": "21", "message": "New comment"}],
                    "total_count": 1,
                    "offset": 0,
                    "limit": 1000,
                }
            return super().get_json(path, params=params, headers=headers)

    client, state = SparseListing(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.partial = True
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert not any(row.get("_deleted") for row in rows)
    assert "box:100:comment:20" in state["documents"]
    assert "box:100:comment:21" in state["documents"]


def test_missing_comment_403_preserves_corpus_and_cursor():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    original = copy.deepcopy(state)
    client.comments["10"] = []
    client.missing_status = 403
    with pytest.raises(BoxSyncError):
        next(sync_box(client, state, folder_ids=["1"]))
    assert state == original


def test_confirmed_comment_delete_with_404_retires_only_comment():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.comments["10"] = []
    client.changed("COMMENT_DELETE", kind="comment", identifier="20")
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert rows == [{"id": "box:100:comment:20", "_deleted": True}]
    assert set(state["documents"]) == {"box:100:folder:1", "box:100:file:10"}


def test_eventless_comment_404_with_accessible_selected_file_retires_only_comment():
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    client.comments["10"] = []
    client.events["p0"] = {"entries": [], "next_stream_position": "p1"}
    rows = list(sync_box(client, state, folder_ids=["1"]))
    assert rows == [{"id": "box:100:comment:20", "_deleted": True}]
    assert set(state["documents"]) == {"box:100:folder:1", "box:100:file:10"}
    assert state["stream_position"] == "p1"


@pytest.mark.parametrize("status", [403, 404])
def test_eventless_comment_404_with_inaccessible_parent_preserves_state(status):
    class ParentUnavailable(FakeBox):
        fail_parent = False

        def get_json(self, path, params=None, headers=None):
            if self.fail_parent and path == "files/10":
                raise BoxSyncError("Parent unavailable", status=status)
            return super().get_json(path, params=params, headers=headers)

    client, state = ParentUnavailable(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    original = copy.deepcopy(state)
    client.comments["10"] = []
    client.fail_parent = True
    with pytest.raises(BoxSyncError):
        next(sync_box(client, state, folder_ids=["1"]))
    assert state == original


@pytest.mark.parametrize("change", ["scope", "identity", "trashed"])
def test_eventless_comment_404_with_unverified_parent_preserves_state(change):
    client, state = FakeBox(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    original = copy.deepcopy(state)
    client.comments["10"] = []
    if change == "scope":
        client.files["10"]["parent"] = {"id": "9"}
        client.files["10"]["path_collection"] = {"entries": [{"id": "0"}, {"id": "9"}]}
    elif change == "identity":
        client.files["10"]["id"] = "11"
    else:
        client.files["10"]["item_status"] = "trashed"
    with pytest.raises(BoxSyncError):
        next(sync_box(client, state, folder_ids=["1"]))
    assert state == original


@pytest.mark.parametrize("status", [403, 404])
def test_comment_delete_event_still_requires_parent_access_after_comment_404(status):
    class ParentLostAccess(FakeBox):
        verify_parent = False
        metadata_reads = 0

        def get_json(self, path, params=None, headers=None):
            if self.verify_parent and path == "files/10":
                self.metadata_reads += 1
                if self.metadata_reads == 2:
                    raise BoxSyncError("Parent access lost after missing comment", status=status)
            return super().get_json(path, params=params, headers=headers)

    client, state = ParentLostAccess(), {}
    list(sync_box(client, state, folder_ids=["1"]))
    original = copy.deepcopy(state)
    client.comments["10"] = []
    client.verify_parent = True
    client.changed("COMMENT_DELETE", kind="comment", identifier="20")
    with pytest.raises(BoxSyncError, match="Parent access lost"):
        next(sync_box(client, state, folder_ids=["1"]))
    assert state == original
