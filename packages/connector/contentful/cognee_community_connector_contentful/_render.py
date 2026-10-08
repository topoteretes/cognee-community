"""Deterministic document rendering, without dereferencing linked resources."""

import json


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(f"- {_value(item)}" for item in value)
    if not isinstance(value, dict):
        return _json(value)
    system = value.get("sys", {})
    if isinstance(system, dict) and system.get("type") in ("Link", "ResourceLink"):
        return f"{system.get('linkType', 'Resource')}: {system.get('id', system.get('urn', ''))}"
    node = value.get("nodeType")
    if node:
        if node == "text":
            text = value.get("value", "")
            marks = [mark.get("type", "") for mark in value.get("marks", [])]
            return f"[{', '.join(marks)}] {text}" if marks else text
        children = value.get("content", [])
        body = "\n".join(_value(child) for child in children)
        data = value.get("data", {})
        details = ""
        if data:
            details = " " + _value(data["target"]) if "target" in data else " " + _json(data)
        return f"[{node}]{details}\n{body}".strip()
    return _json(value)


def row_id(space, environment, kind, upstream_id):
    return f"contentful:{space}:{environment}:{kind}:{upstream_id}"


def document(item, space, environment, locales):
    system = item["sys"]
    kind, upstream_id = system["type"], system["id"]
    lines = [
        "Source: Contentful",
        f"Space: {space}",
        f"Environment: {environment}",
        f"Kind: {kind}",
        f"ID: {upstream_id}",
    ]
    title = f"Contentful {kind}: {upstream_id}"
    url = ""
    if kind == "ContentType":
        title = item.get("name") or title
        for key in ("name", "description", "displayField"):
            if key in item:
                lines.append(f"{key}: {_value(item[key])}")
        for field in item["fields"]:
            if not isinstance(field, dict) or not isinstance(field.get("id"), str):
                raise RuntimeError("Contentful returned an invalid model field.")
            lines.append(f"\nField: {field['id']}")
            for key in sorted(field):
                if key != "id":
                    lines.append(f"{key}: {_value(field[key])}")
    else:
        fields = item.get("fields")
        if not isinstance(fields, dict):
            raise RuntimeError("Contentful returned invalid localized fields.")
        if kind == "Entry":
            content_type = system.get("contentType", {}).get("sys", {}).get("id")
            if not isinstance(content_type, str) or not content_type:
                raise RuntimeError("Contentful entry has no content-type identity.")
            lines.append(f"Content type: {content_type}")
        for field_id in sorted(fields):
            localized = fields[field_id]
            if not isinstance(localized, dict):
                raise RuntimeError("Contentful fields must contain locale maps.")
            for locale in sorted(localized):
                if locales is not None and locale not in locales:
                    continue
                value = localized[locale]
                if kind == "Asset" and field_id == "file":
                    if not isinstance(value, dict):
                        raise RuntimeError("Contentful returned invalid asset metadata.")
                    value = dict(value)
                    asset_url = value.get("url", "")
                    if asset_url.startswith("//"):
                        asset_url = "https:" + asset_url
                    if asset_url.startswith("http://"):
                        asset_url = "https://" + asset_url[len("http://") :]
                    value["url"] = asset_url
                    url = url or asset_url
                lines.append(f"\n{field_id} [{locale}]:\n{_value(value)}")
    return {
        "id": row_id(space, environment, kind, upstream_id),
        "title": title,
        "content": "\n".join(lines),
        "url": url,
        "_deleted": False,
    }
