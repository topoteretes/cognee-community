"""Deterministic document rendering, without dereferencing linked resources."""

import json
from urllib.parse import quote

from .exceptions import ContentfulResponseError


def _invalid(message):
    return ContentfulResponseError(f"Contentful returned {message}.")


def _json(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError):
        raise _invalid("an invalid field value") from None


def _reference(value):
    system = value.get("sys")
    if not isinstance(system, dict):
        raise _invalid("an invalid reference")
    identifier = system.get("id") or system.get("urn")
    kind = system.get("linkType", "Resource")
    if not isinstance(identifier, str) or not identifier or not isinstance(kind, str):
        raise _invalid("an invalid reference")
    return f"{kind}: {identifier}"


def _rich_text(value):
    if not isinstance(value, dict):
        raise _invalid("an invalid rich-text node")
    node = value.get("nodeType")
    if not isinstance(node, str):
        raise _invalid("an invalid rich-text node")
    if node == "text":
        text = value.get("value")
        if not isinstance(text, str):
            raise _invalid("an invalid rich-text value")
        return text
    children = value.get("content", [])
    data = value.get("data", {})
    if (
        not isinstance(children, list)
        or any(not isinstance(child, dict) for child in children)
        or not isinstance(data, dict)
    ):
        raise _invalid("an invalid rich-text structure")
    rendered = [_rich_text(child) for child in children]
    if node in ("unordered-list", "ordered-list"):
        items = []
        for index, child in enumerate(rendered, 1):
            marker = "- " if node == "unordered-list" else f"{index}. "
            lines = child.splitlines() or [""]
            items.append(
                marker
                + lines[0]
                + "".join("\n" + (" " * len(marker) + line if line else "") for line in lines[1:])
            )
        return "\n".join(items)
    if node == "list-item":
        body = ""
        for child, text in zip(children, rendered, strict=True):
            separator = (
                "\n" if child.get("nodeType") in ("unordered-list", "ordered-list") else "\n\n"
            )
            if text:
                body += (separator if body else "") + text
    elif node in ("table-cell", "table-header-cell"):
        # Keep multi-paragraph cells inside their row in plain-text tables.
        body = " / ".join(" / ".join(child.splitlines()) for child in rendered if child)
    elif node in ("document", "blockquote"):
        body = "\n\n".join(child for child in rendered if child)
    elif node == "table":
        body = "\n".join(rendered)
    elif node == "table-row":
        body = " | ".join(rendered)
    elif node in (
        "paragraph",
        "heading-1",
        "heading-2",
        "heading-3",
        "heading-4",
        "heading-5",
        "heading-6",
        "hyperlink",
        "entry-hyperlink",
        "asset-hyperlink",
        "resource-hyperlink",
        "embedded-entry-inline",
        "embedded-entry-block",
        "embedded-asset-block",
        "embedded-resource-inline",
        "embedded-resource-block",
        "hr",
    ):
        # Text and inline nodes inside a paragraph must retain their adjacency.
        body = "".join(rendered)
    else:
        raise _invalid("an unsupported rich-text node")
    if node == "hyperlink":
        uri = data.get("uri")
        if not isinstance(uri, str):
            raise _invalid("an invalid rich-text hyperlink")
        return body if body == uri else f"{body} ({uri})" if body else uri
    if "target" in data:
        target = data["target"]
        if not isinstance(target, dict):
            raise _invalid("an invalid rich-text reference")
        reference = _reference(target)
        result = f"{body} ({reference})" if body else reference
        return (
            f" {result} "
            if node in ("embedded-entry-inline", "embedded-resource-inline")
            else result
        )
    return "---" if node == "hr" else body


def _value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(f"- {_value(item)}" for item in value)
    if not isinstance(value, dict):
        return _json(value)
    system = value.get("sys", {})
    if isinstance(system, dict) and system.get("type") in ("Link", "ResourceLink"):
        return _reference(value)
    return _json(value)


def _field_value(value, field):
    if field.get("type") != "RichText":
        return _value(value)
    try:
        return _rich_text(value)
    except (ContentfulResponseError, RecursionError):
        # Preserve the source value and other entities instead of repeatedly
        # failing the whole extraction or silently dropping an unknown node.
        return "[Invalid RichText; preserved JSON]\n" + _json(value)


def _localized(fields):
    if not isinstance(fields, dict):
        raise _invalid("invalid localized fields")
    for field_id, localized in fields.items():
        if (
            not isinstance(field_id, str)
            or not field_id
            or not isinstance(localized, dict)
            or any(not isinstance(locale, str) or not locale for locale in localized)
        ):
            raise _invalid("invalid locale maps")
    return fields


def _field_locales(localized, locales, default_locale, field, *, asset=False):
    if locales is None:
        return sorted(localized, key=lambda locale: (locale != default_locale, locale))
    selected = [locale for locale in locales if locale in localized]
    # Entry localization is a schema property, not a count of translations.
    if field.get("localized") is False and default_locale in localized:
        return [default_locale]
    # Assets have no content-type field schema; retain their single-locale metadata.
    return selected or (list(localized) if asset and len(localized) == 1 else [])


def _display_title(fields, display_field, locales, fallback, default_locale, field, *, asset=False):
    localized = fields.get(display_field, {})
    for locale in _field_locales(localized, locales, default_locale, field, asset=asset):
        title = localized.get(locale)
        if isinstance(title, str) and title.strip():
            return title.strip()
    return fallback


def row_id(space, environment, kind, upstream_id):
    return f"contentful:{space}:{environment}:{kind}:{upstream_id}"


def document(
    item,
    space,
    environment,
    locales,
    models=None,
    *,
    default_locale=None,
    host="cdn.contentful.com",
):
    """Render an entity using the current content-model inventory for its title."""
    if models is not None and not isinstance(models, dict):
        raise _invalid("an invalid content-model inventory")
    if not isinstance(item, dict) or not isinstance(item.get("sys"), dict):
        raise _invalid("an invalid document identity")
    system = item["sys"]
    kind, upstream_id = system.get("type"), system.get("id")
    if (
        kind not in ("Entry", "Asset", "ContentType")
        or not isinstance(upstream_id, str)
        or not upstream_id
    ):
        raise _invalid("an invalid document identity")
    lines = []
    title = upstream_id
    url = ""
    content_type = None
    field_models = {}
    if kind == "ContentType":
        if not isinstance(item.get("name", ""), str):
            raise _invalid("an invalid model name")
        title = item.get("name") or upstream_id
        for key in ("name", "description", "displayField"):
            if key in item:
                lines.append(f"{key}: {_value(item[key])}")
        fields = item.get("fields")
        if not isinstance(fields, list) or any(
            not isinstance(field, dict) or not isinstance(field.get("id"), str) or not field["id"]
            for field in fields
        ):
            raise _invalid("invalid model fields")
        if len({field["id"] for field in fields}) != len(fields):
            raise _invalid("duplicate model fields")
        for field in sorted(fields, key=lambda field: field["id"]):
            if any(not isinstance(key, str) for key in field):
                raise _invalid("an invalid model field")
            lines.append(f"\nField: {field['id']}")
            for key in sorted(field):
                if key != "id":
                    lines.append(f"{key}: {_value(field[key])}")
    else:
        fields = _localized(item.get("fields"))
        if kind == "Entry":
            content_type_link = system.get("contentType")
            content_type_sys = (
                content_type_link.get("sys") if isinstance(content_type_link, dict) else None
            )
            content_type = (
                content_type_sys.get("id") if isinstance(content_type_sys, dict) else None
            )
            if not isinstance(content_type, str) or not content_type:
                raise _invalid("an entry without a content-type identity")
            model = models.get(content_type, {}) if models is not None else {}
            if not isinstance(model, dict):
                raise _invalid("an invalid content model")
            display_field = model.get("displayField")
            if display_field is not None and not isinstance(display_field, str):
                raise _invalid("an invalid display field")
            field_models = {field["id"]: field for field in model.get("fields", [])}
            title = _display_title(
                fields,
                display_field,
                locales,
                upstream_id,
                default_locale,
                field_models.get(display_field, {}),
            )
            editor = (
                "app.eu.contentful.com" if host == "cdn.eu.contentful.com" else "app.contentful.com"
            )
            url = (
                f"https://{editor}/spaces/{quote(space, safe='')}"
                f"/environments/{quote(environment, safe='')}/entries/{quote(upstream_id, safe='')}"
            )
        else:
            title = _display_title(
                fields, "title", locales, upstream_id, default_locale, {}, asset=True
            )
        for field_id in sorted(fields):
            localized = fields[field_id]
            for locale in _field_locales(
                localized,
                locales,
                default_locale,
                field_models.get(field_id, {}),
                asset=kind == "Asset",
            ):
                value = localized[locale]
                if kind == "Asset" and field_id == "file":
                    if not isinstance(value, dict):
                        raise _invalid("invalid asset metadata")
                    value = dict(value)
                    asset_url = value.get("url", "")
                    if not isinstance(asset_url, str):
                        raise _invalid("an invalid asset URL")
                    if asset_url.startswith("//"):
                        asset_url = "https:" + asset_url
                    if asset_url.startswith("http://"):
                        asset_url = "https://" + asset_url[len("http://") :]
                    value["url"] = asset_url
                    url = url or asset_url
                rendered_value = _field_value(value, field_models.get(field_id, {}))
                lines.append(f"\n{field_id} [{locale}]:\n{rendered_value}")
    provenance = [
        "Source: Contentful",
        f"Space: {space}",
        f"Environment: {environment}",
        f"Kind: {kind}",
        f"ID: {upstream_id}",
    ]
    if content_type is not None:
        provenance.append(f"Content type: {content_type}")
    return {
        "id": row_id(space, environment, kind, upstream_id),
        "title": title,
        "content": "\n".join([title, *lines, "", *provenance]),
        "url": url,
        "_deleted": False,
    }
