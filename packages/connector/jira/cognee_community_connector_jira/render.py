"""Deterministic Jira prose; source URLs are context, never fetched here."""

from urllib.parse import quote

from .transport import numeric_id


def adf_text(value, depth: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if depth > 40 or not isinstance(value, dict):
        raise ValueError("Invalid or overly nested Jira document")
    kind = value.get("type")
    attrs = value.get("attrs", {})
    children = value.get("content", [])
    if not isinstance(kind, str) or not isinstance(attrs, dict) or not isinstance(children, list):
        raise ValueError("Invalid Jira document structure")
    if kind == "text":
        text = value.get("text")
        if not isinstance(text, str) or not isinstance(value.get("marks", []), list):
            raise ValueError("Invalid Jira text")
        for mark in value.get("marks", []):
            if not isinstance(mark, dict):
                raise ValueError("Invalid Jira text mark")
            if mark.get("type") == "link":
                attrs = mark.get("attrs")
                if not isinstance(attrs, dict) or not isinstance(attrs.get("href"), str):
                    raise ValueError("Invalid Jira link mark")
                link = attrs["href"]
                text = f"{text} ({link})"
            elif mark.get("type") == "strike":
                text = f"~~{text}~~"
            elif mark.get("type") == "code":
                text = f"`{text}`"
            elif mark.get("type") == "subsup":
                attrs = mark.get("attrs")
                if not isinstance(attrs, dict) or attrs.get("type") not in {"sup", "sub"}:
                    raise ValueError("Invalid Jira superscript/subscript mark")
                prefix = "^" if attrs["type"] == "sup" else "_"
                text = f"{prefix}{{{text}}}"
            elif mark.get("type") == "strong":
                text = f"**{text}**"
            elif mark.get("type") == "em":
                text = f"*{text}*"
            elif mark.get("type") == "underline":
                text = f"<u>{text}</u>"
            elif mark.get("type") not in {"textColor", "backgroundColor", "border"}:
                text = f"[Unsupported ADF mark: {mark.get('type')}] {text}"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind in {"inlineCard", "blockCard", "embedCard"}:
        url = attrs.get("url")
        if not isinstance(url, str) or not url:
            # Resolved card data can omit the original URL; keep the limitation visible.
            return f"[{kind}: URL unavailable]"
        return url + ("\n" if kind != "inlineCard" else "")
    if kind in {"mention", "emoji", "status", "date"}:
        return str(
            attrs.get("text")
            or attrs.get("shortName")
            or attrs.get("url")
            or attrs.get("timestamp")
            or attrs.get("id")
            or ""
        )
    if kind in {"media", "mediaInline"}:
        return f"[Embedded media: {attrs.get('alt') or attrs.get('id') or 'see attachments'}]"
    texts = [adf_text(child, depth + 1) for child in children]
    text = "".join(texts)
    if kind in {"orderedList", "bulletList"}:
        start = attrs.get("order", 1)
        if type(start) is not int or start < 1:
            raise ValueError("Invalid ordered-list start")
        return "".join(
            f"{start + index}. {part.strip()}\n" if kind == "orderedList" else f"- {part.strip()}\n"
            for index, part in enumerate(texts)
        )
    if kind == "heading":
        level = attrs.get("level", 1)
        if type(level) is not int or not 1 <= level <= 6:
            raise ValueError("Invalid heading level")
        return f"{'#' * level} {text}\n"
    if kind == "codeBlock":
        return f"```{attrs.get('language', '')}\n{text}\n```\n"
    if kind in {"expand", "nestedExpand"}:
        return f"{attrs.get('title', '')}\n{text}"
    if kind in {"paragraph", "blockquote", "tableRow", "listItem", "caption"}:
        return text + "\n"
    if kind in {"tableCell", "tableHeader"}:
        return text.strip() + " | "
    if kind == "rule":
        return "---\n"
    if kind in {"doc", "table", "panel", "mediaSingle", "mediaGroup"}:
        return text
    return f"[Unsupported ADF node: {kind}]\n{text}"


def named(value) -> str:
    if isinstance(value, dict):
        return str(
            value.get("displayName")
            or value.get("name")
            or value.get("key")
            or value.get("accountId")
            or value.get("id")
            or ""
        )
    return str(value or "")


def issue_row(issue: dict, site_url: str) -> dict:
    fields = issue["fields"]
    identity, key = numeric_id(issue["id"]), issue["key"]
    url = f"{site_url}/browse/{quote(key, safe='')}"
    lines = [f"Jira issue: {key}", f"Issue ID: {identity}", f"Source: {url}"]
    for label, field in (
        ("Project", "project"),
        ("Status", "status"),
        ("Assignee", "assignee"),
        ("Reporter", "reporter"),
        ("Priority", "priority"),
        ("Type", "issuetype"),
        ("Parent", "parent"),
    ):
        lines.append(f"{label}: {named(fields.get(field))}")
    labels = fields.get("labels") or []
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        raise ValueError("Invalid Jira labels")
    lines.append(f"Labels: {', '.join(sorted(labels))}")
    return {
        "id": f"issue:{identity}",
        "title": f"{key}: {fields['summary']}",
        "url": url,
        "content": "\n".join(lines) + "\n\n" + adf_text(fields["description"]).strip(),
    }


def comment_row(comment: dict, parent: dict) -> dict:
    identity = numeric_id(comment["id"])
    return {
        "id": f"{parent['id']}:comment:{identity}",
        "title": f"Comment {identity} on {parent['title']}",
        "url": f"{parent['url']}?focusedCommentId={identity}",
        "content": (
            f"Parent: {parent['title']}\nSource: {parent['url']}\nComment ID: {identity}\n"
            f"Author: {named(comment.get('author'))}\nCreated: {comment.get('created', '')}\n\n"
            + adf_text(comment["body"]).strip()
        ),
    }


def attachment_row(attachment: dict, parent: dict, text: str) -> dict:
    identity = numeric_id(attachment["id"])
    return {
        "id": f"{parent['id']}:attachment:{identity}",
        "title": f"{attachment['filename']} on {parent['title']}",
        "url": parent["url"],
        "content": (
            f"Parent: {parent['title']}\nSource: {parent['url']}\nAttachment ID: {identity}\n"
            f"Filename: {attachment['filename']}\nMIME type: {attachment['mimeType']}\n\n{text}"
        ),
    }
