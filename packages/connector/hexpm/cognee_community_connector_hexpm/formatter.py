"""Format Hex.pm package entities into structured Markdown and Cognee documents."""

import hashlib
from typing import Any

from cognee_community_connector_hexpm.models import HexPackage, RenderOptions


def render_hex_markdown(
    pkg: HexPackage,
    options: RenderOptions | None = None,
) -> str:
    """Render a HexPackage entity into structured Markdown for Cognee indexing."""
    opts = options or RenderOptions()
    rel = pkg.latest_release
    ver_str = rel.version if rel else pkg.latest_version

    lines: list[str] = [
        f"# Hex Package: {pkg.name} (v{ver_str})",
        "",
        "## Summary",
        f"- **Name:** `{pkg.name}`",
        f"- **Latest Version:** `{ver_str}`",
        f"- **Description:** {pkg.description or 'No description provided.'}",
        f"- **Licenses:** {', '.join(pkg.licenses) if pkg.licenses else 'Not specified'}",
        f"- **HexDocs:** {pkg.docs_html_url or f'https://hexdocs.pm/{pkg.name}'}",
        f"- **Package URL:** {pkg.html_url or f'https://hex.pm/packages/{pkg.name}'}",
    ]

    if pkg.maintainers:
        lines.append(f"- **Maintainers:** {', '.join(pkg.maintainers)}")

    if pkg.links:
        link_items = [f"[{k}]({v})" for k, v in sorted(pkg.links.items())]
        lines.append(f"- **Links:** {', '.join(link_items)}")

    lines.append("")

    # Prominent warning if the current/latest release is retired
    if opts.include_retirements and rel and rel.retirement:
        ret = rel.retirement
        lines.extend(
            [
                "> [!WARNING]",
                f"> ⚠️ **RETIRED RELEASE:** Version `{ret.version}` has been retired upstream.",
                f"> **Reason:** `{ret.reason}`",
                f"> **Notice:** {ret.message or 'No additional details provided by author.'}",
                "",
            ]
        )

    # All known historical retirements table
    if opts.include_retirements and pkg.all_retirements:
        lines.extend(
            [
                "## Upstream Retirements History",
                "| Version | Retirement Reason | Author Message |",
                "| :--- | :--- | :--- |",
            ]
        )
        for v_str, r_obj in sorted(pkg.all_retirements.items()):
            msg_clean = r_obj.message.replace("\n", " ") if r_obj.message else "*(none)*"
            lines.append(f"| `{v_str}` | `{r_obj.reason}` | {msg_clean} |")
        lines.append("")

    # Release dependencies table
    if opts.include_dependencies and rel and rel.requirements:
        lines.extend(
            [
                "## Dependencies",
                "| Dependency | App | Requirement | Optional |",
                "| :--- | :--- | :--- | :--- |",
            ]
        )
        for dep in sorted(rel.requirements, key=lambda d: (d.optional, d.name)):
            opt_str = "Yes" if dep.optional else "No"
            lines.append(f"| `{dep.name}` | `{dep.app}` | `{dep.requirement}` | {opt_str} |")
        lines.append("")

    # Security advisories if any
    if opts.include_security_advisories and rel and rel.security_advisories:
        lines.extend(
            [
                "## Security Advisories",
                "| Advisory ID | Title | Patched In |",
                "| :--- | :--- | :--- |",
            ]
        )
        for adv in rel.security_advisories:
            adv_id = adv.get("id", "Unknown")
            title = adv.get("title", "").replace("\n", " ")
            patched = ", ".join(adv.get("patched_versions", [])) or "None"
            lines.append(f"| `{adv_id}` | {title} | `{patched}` |")
        lines.append("")

    return "\n".join(lines).strip()


def hex_to_cognee_document(
    pkg: HexPackage,
    options: RenderOptions | None = None,
) -> dict[str, Any]:
    """Convert HexPackage into a first-class Cognee document payload with tombstones."""
    opts = options or RenderOptions()
    markdown_content = render_hex_markdown(pkg, options=opts)
    ver_str = pkg.latest_release.version if pkg.latest_release else pkg.latest_version

    # Deterministic SHA-256 content hash
    hasher = hashlib.sha256()
    hasher.update(markdown_content.encode("utf-8"))
    content_hash = hasher.hexdigest()

    # Tombstone logic for retired or removed packages
    is_deleted = False
    if opts.exclude_retired and pkg.latest_release and pkg.latest_release.retirement:
        is_deleted = True

    return {
        "id": f"hexpm:{pkg.name}",
        "title": f"Hex Package: {pkg.name} (v{ver_str})",
        "content": markdown_content,
        "url": pkg.html_url or f"https://hex.pm/packages/{pkg.name}",
        "source": "hexpm",
        "data_id": content_hash,
        "version": ver_str,
        "_deleted": is_deleted,
    }
