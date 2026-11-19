"""Format Crates.io packages into structured Markdown and Cognee documents."""

import hashlib
from typing import Any

from cognee_community_connector_crates.models import CratePackage, RenderOptions


def render_crate_markdown(
    pkg: CratePackage,
    options: RenderOptions | None = None,
) -> str:
    """Render a CratePackage into structured Markdown for Cognee NLP indexing."""
    opts = options or RenderOptions()
    meta = pkg.metadata
    ver = pkg.latest_version
    ver_str = ver.num if ver else meta.max_version

    lines: list[str] = [
        f"# Rust Crate: {meta.name} (v{ver_str})",
        "",
        "## Summary",
        f"- **Name:** `{meta.name}`",
        f"- **Latest Version:** `{ver_str}`",
        f"- **Description:** {meta.description or 'No description provided.'}",
        f"- **License:** {ver.license if ver and ver.license else 'Not specified'}",
        f"- **Total Downloads:** {meta.downloads:,}",
    ]

    if meta.documentation:
        lines.append(f"- **Documentation:** {meta.documentation}")
    else:
        lines.append(f"- **Documentation:** https://docs.rs/{meta.name}/{ver_str}")

    if meta.repository:
        lines.append(f"- **Repository:** {meta.repository}")
    if meta.homepage:
        lines.append(f"- **Homepage:** {meta.homepage}")

    if meta.keywords:
        kw_list = [f"`{k}`" for k in meta.keywords]
        lines.append(f"- **Keywords:** {', '.join(kw_list)}")

    if meta.categories:
        cat_list = [f"`{c}`" for c in meta.categories]
        lines.append(f"- **Categories:** {', '.join(cat_list)}")

    lines.append("")

    # Feature flags
    if opts.include_features and ver and ver.features:
        lines.extend(
            [
                "## Cargo Feature Flags",
                "| Feature | Enables / Sub-features |",
                "| :--- | :--- |",
            ]
        )
        for feat_name, sub_feats in sorted(ver.features.items()):
            sub_str = ", ".join(f"`{s}`" for s in sub_feats) if sub_feats else "*(standalone)*"
            lines.append(f"| `{feat_name}` | {sub_str} |")
        lines.append("")

    # Dependencies
    if opts.include_dependencies and pkg.dependencies:
        lines.extend(
            [
                "## Dependencies",
                "| Dependency | Requirement | Kind | Optional |",
                "| :--- | :--- | :--- | :--- |",
            ]
        )
        for dep in sorted(pkg.dependencies, key=lambda d: (d.kind, d.crate_id)):
            opt_str = "Yes" if dep.optional else "No"
            lines.append(f"| `{dep.crate_id}` | `{dep.req}` | `{dep.kind}` | {opt_str} |")
        lines.append("")

    # README content
    if opts.include_readme and pkg.readme:
        readme_clean = pkg.readme.strip()
        if len(readme_clean) > opts.max_readme_chars:
            readme_clean = (
                readme_clean[: opts.max_readme_chars]
                + f"\n\n*(Truncated at {opts.max_readme_chars} characters)*"
            )
        lines.extend(
            [
                "## Documentation / README",
                "",
                readme_clean,
                "",
            ]
        )

    return "\n".join(lines).strip()


def crate_to_cognee_document(
    pkg: CratePackage,
    options: RenderOptions | None = None,
) -> dict[str, Any]:
    """Convert CratePackage into a first-class Cognee document payload."""
    markdown_content = render_crate_markdown(pkg, options=options)
    meta = pkg.metadata
    ver_str = pkg.latest_version.num if pkg.latest_version else meta.max_version

    # Deterministic content hash prevents re-cognifying unchanged crates
    hasher = hashlib.sha256()
    hasher.update(markdown_content.encode("utf-8"))
    content_hash = hasher.hexdigest()

    return {
        "id": f"crates:{meta.name}",
        "title": f"Rust Crate: {meta.name} (v{ver_str})",
        "content": markdown_content,
        "url": f"https://crates.io/crates/{meta.name}",
        "source": "crates",
        "data_id": content_hash,
        "version": ver_str,
        "downloads": meta.downloads,
        "_deleted": False,
    }
