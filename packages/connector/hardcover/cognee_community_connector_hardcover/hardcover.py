from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp


@dataclass
class HardcoverJournal:
    id: str
    book_id: str
    book_title: str
    entry_date: str
    note: str
    progress_pages: Optional[int] = None
    progress_percentage: Optional[float] = None
    is_private: bool = False

    def to_markdown(self) -> str:
        privacy_tag = " [PRIVATE NOTE]" if self.is_private else ""
        lines = [
            f"### Reading Journal: {self.book_title}{privacy_tag}",
            f"- **Date:** {self.entry_date}",
        ]
        if self.progress_pages is not None:
            lines.append(f"- **Page Progress:** {self.progress_pages}")
        if self.progress_percentage is not None:
            lines.append(f"- **Completion:** {self.progress_percentage:.1f}%")
        lines.append(f"\n{self.note}")
        return "\n".join(lines)


@dataclass
class HardcoverBook:
    id: str
    title: str
    authors: List[str]
    status: str
    subtitle: Optional[str] = None
    pages: Optional[int] = None
    release_year: Optional[int] = None
    rating: Optional[float] = None
    review: Optional[str] = None
    updated_at: str = ""
    journals: List[HardcoverJournal] = field(default_factory=list)

    @property
    def display_authors(self) -> str:
        return ", ".join(self.authors) if self.authors else "Unknown Author"

    def to_markdown(self) -> str:
        lines = [
            f"# Book: {self.title}",
            f"- **Author(s):** {self.display_authors}",
            f"- **Reading Status:** {self.status}",
        ]
        if self.subtitle:
            lines.append(f"- **Subtitle:** {self.subtitle}")
        if self.pages:
            lines.append(f"- **Pages:** {self.pages}")
        if self.release_year:
            lines.append(f"- **Published:** {self.release_year}")
        if self.rating is not None:
            lines.append(f"- **Personal Rating:** {self.rating} / 5.0")
        if self.updated_at:
            lines.append(f"- **Last Updated:** {self.updated_at}")
        if self.review:
            lines.append(f"\n### Personal Review:\n{self.review}")
        if self.journals:
            lines.append("\n### Reading Journal Log:")
            for j in self.journals:
                lines.append(f"- [{j.entry_date}] (Page {j.progress_pages or 'N/A'}): {j.note}")
        return "\n".join(lines)


@dataclass
class HardcoverList:
    id: str
    name: str
    description: str = ""
    book_count: int = 0
    books: List[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Reading List: {self.name}",
            f"- **List ID:** {self.id}",
            f"- **Book Count:** {self.book_count}",
        ]
        if self.description:
            lines.append(f"- **Description:** {self.description}")
        if self.books:
            lines.append("\n### Books in List:")
            for b in self.books:
                lines.append(f"- {b}")
        return "\n".join(lines)


class HardcoverConnector:
    """Cognee community connector for Hardcover GraphQL reading and book tracker API."""

    def __init__(
        self,
        token: str,
        api_url: str = "https://api.hardcover.app/v1/graphql",
        include_private: bool = False,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.token = token.strip()
        self.api_url = api_url
        self.include_private = include_private
        self._session = session
        self._owns_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "Cognee-Connector-Hardcover/0.1.0",
        }
        payload = {"query": query, "variables": variables or {}}

        async with session.post(self.api_url, json=payload, headers=headers) as response:
            if response.status == 401:
                data = await response.json()
                error_msg = data.get("error", "invalid_token") if isinstance(data, dict) else "Unauthorized"
                raise PermissionError(f"Hardcover authentication failed: {error_msg}")
            if response.status == 429:
                raise RuntimeError("Hardcover API rate limit exceeded (HTTP 429)")
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"Hardcover GraphQL HTTP {response.status} error: {text}")

            result = await response.json()
            if "errors" in result and result["errors"]:
                raise RuntimeError(f"Hardcover GraphQL error: {result['errors'][0].get('message', 'Unknown error')}")
            return result.get("data", {})

    async def fetch_me(self) -> Dict[str, Any]:
        """Fetch authenticated user profile details."""
        query = """
        query GetMe {
            me {
                id
                username
                name
            }
        }
        """
        data = await self._graphql(query)
        me_records = data.get("me", [])
        if isinstance(me_records, list) and me_records:
            return me_records[0]
        elif isinstance(me_records, dict):
            return me_records
        return {}

    async def fetch_user_books(self, limit: int = 50) -> List[HardcoverBook]:
        """Fetch user books library with authors, statuses, and ratings."""
        query = """
        query GetUserBooks($limit: Int!) {
            user_books(order_by: { updated_at: desc }, limit: $limit) {
                id
                status_id
                rating
                review
                updated_at
                book {
                    id
                    title
                    subtitle
                    pages
                    release_year
                    contributions {
                        author {
                            name
                        }
                    }
                }
            }
        }
        """
        data = await self._graphql(query, variables={"limit": limit})
        user_books = data.get("user_books", [])

        status_map = {
            1: "Want to Read",
            2: "Currently Reading",
            3: "Read",
            4: "Did Not Finish",
            5: "Owned",
        }

        books = []
        for ub in user_books:
            b_info = ub.get("book") or {}
            authors = []
            for contrib in b_info.get("contributions", []):
                a_info = contrib.get("author") or {}
                if a_info.get("name"):
                    authors.append(a_info["name"])

            raw_status = ub.get("status_id")
            status_label = status_map.get(raw_status, f"Status #{raw_status}")

            book = HardcoverBook(
                id=str(ub.get("id", "")),
                title=b_info.get("title", "Untitled Book"),
                subtitle=b_info.get("subtitle"),
                authors=authors,
                status=status_label,
                pages=b_info.get("pages"),
                release_year=b_info.get("release_year"),
                rating=float(ub["rating"]) if ub.get("rating") is not None else None,
                review=ub.get("review"),
                updated_at=ub.get("updated_at", ""),
            )
            books.append(book)

        return books

    async def fetch_journals(self, limit: int = 50) -> List[HardcoverJournal]:
        """Fetch reading journal logs, honoring the private note toggle."""
        query = """
        query GetUserJournals($limit: Int!) {
            reading_journals(order_by: { entry_date: desc }, limit: $limit) {
                id
                entry_date
                note
                progress_pages
                progress_percentage
                is_private
                user_book {
                    book {
                        id
                        title
                    }
                }
            }
        }
        """
        data = await self._graphql(query, variables={"limit": limit})
        entries = data.get("reading_journals", [])

        journals = []
        for item in entries:
            is_priv = bool(item.get("is_private", False))
            if is_priv and not self.include_private:
                continue

            ub = item.get("user_book") or {}
            b = ub.get("book") or {}

            journal = HardcoverJournal(
                id=str(item.get("id", "")),
                book_id=str(b.get("id", "")),
                book_title=b.get("title", "Unknown Book"),
                entry_date=item.get("entry_date", ""),
                note=item.get("note", ""),
                progress_pages=item.get("progress_pages"),
                progress_percentage=item.get("progress_percentage"),
                is_private=is_priv,
            )
            journals.append(journal)

        return journals

    async def fetch_lists(self, limit: int = 20) -> List[HardcoverList]:
        """Fetch user reading lists."""
        query = """
        query GetLists($limit: Int!) {
            lists(order_by: { updated_at: desc }, limit: $limit) {
                id
                name
                description
                books_count
                list_books {
                    book {
                        title
                    }
                }
            }
        }
        """
        data = await self._graphql(query, variables={"limit": limit})
        raw_lists = data.get("lists", [])

        lists = []
        for l in raw_lists:
            titles = []
            for lb in l.get("list_books", []):
                bk = lb.get("book") or {}
                if bk.get("title"):
                    titles.append(bk["title"])

            lst = HardcoverList(
                id=str(l.get("id", "")),
                name=l.get("name", "Untitled List"),
                description=l.get("description", ""),
                book_count=int(l.get("books_count", len(titles))),
                books=titles,
            )
            lists.append(lst)

        return lists

    async def to_documents(
        self,
        include_books: bool = True,
        include_journals: bool = True,
        include_lists: bool = True,
        limit: int = 50,
    ) -> List[str]:
        """Generate structured markdown documents for Cognee memory ingestion."""
        docs = []
        if include_books:
            books = await self.fetch_user_books(limit=limit)
            for b in books:
                docs.append(b.to_markdown())

        if include_journals:
            journals = await self.fetch_journals(limit=limit)
            for j in journals:
                docs.append(j.to_markdown())

        if include_lists:
            lists = await self.fetch_lists(limit=limit)
            for l in lists:
                docs.append(l.to_markdown())

        return docs
