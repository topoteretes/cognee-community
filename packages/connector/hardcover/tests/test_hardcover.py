import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cognee_community_connector_hardcover.hardcover import (
    HardcoverConnector,
    HardcoverBook,
    HardcoverJournal,
    HardcoverList,
)


@pytest.fixture
def api_token():
    return "hc_pat_test_token_abcdef123456"


def test_fetch_me(api_token):
    async def run_test():
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={"data": {"me": [{"id": "user_42", "username": "bookworm", "name": "Elena Vance"}]}}
        )
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        mock_session.closed = False

        connector = HardcoverConnector(token=api_token, session=mock_session)
        me = await connector.fetch_me()
        assert me["id"] == "user_42"
        assert me["username"] == "bookworm"
        assert me["name"] == "Elena Vance"

    asyncio.run(run_test())


def test_fetch_user_books(api_token):
    async def run_test():
        books_data = {
            "data": {
                "user_books": [
                    {
                        "id": "ub_101",
                        "status_id": 3,
                        "rating": "4.5",
                        "review": "A masterpiece in modern worldbuilding and hard magic systems.",
                        "updated_at": "2026-10-20T12:00:00Z",
                        "book": {
                            "id": "b_500",
                            "title": "The Way of Kings",
                            "subtitle": "The Stormlight Archive Book 1",
                            "pages": 1007,
                            "release_year": 2010,
                            "contributions": [{"author": {"name": "Brandon Sanderson"}}],
                        },
                    }
                ]
            }
        }

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=books_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        mock_session.closed = False

        connector = HardcoverConnector(token=api_token, session=mock_session)
        books = await connector.fetch_user_books()
        assert len(books) == 1
        b = books[0]
        assert b.title == "The Way of Kings"
        assert b.display_authors == "Brandon Sanderson"
        assert b.status == "Read"
        assert b.rating == 4.5
        assert b.pages == 1007

        md = b.to_markdown()
        assert "# Book: The Way of Kings" in md
        assert "**Author(s):** Brandon Sanderson" in md
        assert "**Personal Rating:** 4.5 / 5.0" in md
        assert "A masterpiece in modern worldbuilding" in md

    asyncio.run(run_test())


def test_fetch_journals_privacy_filter(api_token):
    async def run_test():
        journals_data = {
            "data": {
                "reading_journals": [
                    {
                        "id": "j_1",
                        "entry_date": "2026-10-18",
                        "note": "Public chapter 10 commentary on bridge runs.",
                        "progress_pages": 250,
                        "progress_percentage": 24.8,
                        "is_private": False,
                        "user_book": {"book": {"id": "b_500", "title": "The Way of Kings"}},
                    },
                    {
                        "id": "j_2",
                        "entry_date": "2026-10-19",
                        "note": "Private personal thoughts on character themes.",
                        "progress_pages": 310,
                        "progress_percentage": 30.7,
                        "is_private": True,
                        "user_book": {"book": {"id": "b_500", "title": "The Way of Kings"}},
                    },
                ]
            }
        }

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=journals_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        mock_session.closed = False

        # 1. With include_private=False (default)
        connector_pub = HardcoverConnector(token=api_token, include_private=False, session=mock_session)
        journals_pub = await connector_pub.fetch_journals()
        assert len(journals_pub) == 1
        assert journals_pub[0].id == "j_1"
        assert "Public chapter 10" in journals_pub[0].note

        # 2. With include_private=True
        connector_priv = HardcoverConnector(token=api_token, include_private=True, session=mock_session)
        journals_priv = await connector_priv.fetch_journals()
        assert len(journals_priv) == 2
        assert any(j.is_private for j in journals_priv)
        assert "[PRIVATE NOTE]" in journals_priv[1].to_markdown()

    asyncio.run(run_test())


def test_fetch_lists(api_token):
    async def run_test():
        lists_data = {
            "data": {
                "lists": [
                    {
                        "id": "lst_99",
                        "name": "Hugo Award Winners",
                        "description": "All time favorite speculative fiction.",
                        "books_count": 2,
                        "list_books": [{"book": {"title": "Dune"}}, {"book": {"title": "Neuromancer"}}],
                    }
                ]
            }
        }

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=lists_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        mock_session.closed = False

        connector = HardcoverConnector(token=api_token, session=mock_session)
        lists = await connector.fetch_lists()
        assert len(lists) == 1
        lst = lists[0]
        assert lst.name == "Hugo Award Winners"
        assert lst.books == ["Dune", "Neuromancer"]

        md = lst.to_markdown()
        assert "# Reading List: Hugo Award Winners" in md
        assert "- Dune" in md
        assert "- Neuromancer" in md

    asyncio.run(run_test())


def test_to_documents(api_token):
    async def run_test():
        mock_session = MagicMock()

        def mock_post(url, json=None, headers=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            q = json.get("query", "") if json else ""
            if "user_books" in q:
                mock_resp.json = AsyncMock(
                    return_value={
                        "data": {
                            "user_books": [
                                {
                                    "id": "ub_1",
                                    "status_id": 3,
                                    "rating": 5.0,
                                    "book": {
                                        "title": "Foundation",
                                        "contributions": [{"author": {"name": "Isaac Asimov"}}],
                                    },
                                }
                            ]
                        }
                    }
                )
            elif "reading_journals" in q:
                mock_resp.json = AsyncMock(
                    return_value={
                        "data": {
                            "reading_journals": [
                                {
                                    "id": "j_1",
                                    "entry_date": "2026-10-21",
                                    "note": "Psychohistory concept is brilliant.",
                                    "user_book": {"book": {"id": "b_1", "title": "Foundation"}},
                                }
                            ]
                        }
                    }
                )
            elif "lists" in q:
                mock_resp.json = AsyncMock(
                    return_value={
                        "data": {
                            "lists": [
                                {
                                    "id": "l_1",
                                    "name": "Classic Sci-Fi",
                                    "books_count": 1,
                                    "list_books": [{"book": {"title": "Foundation"}}],
                                }
                            ]
                        }
                    }
                )
            else:
                mock_resp.json = AsyncMock(return_value={"data": {}})

            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.post.side_effect = mock_post
        mock_session.closed = False

        connector = HardcoverConnector(token=api_token, session=mock_session)
        docs = await connector.to_documents()
        assert len(docs) == 3
        assert any("# Book: Foundation" in d for d in docs)
        assert any("Reading Journal: Foundation" in d for d in docs)
        assert any("# Reading List: Classic Sci-Fi" in d for d in docs)

    asyncio.run(run_test())


def test_auth_error_handling(api_token):
    async def run_test():
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 401
        mock_resp.json = AsyncMock(return_value={"error": "invalid_token"})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        mock_session.closed = False

        connector = HardcoverConnector(token=api_token, session=mock_session)
        with pytest.raises(PermissionError, match="Hardcover authentication failed: invalid_token"):
            await connector.fetch_me()

    asyncio.run(run_test())
