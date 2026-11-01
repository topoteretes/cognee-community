# Cognee Connector: NuGet v3 (.NET Package Registry)

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that syncs .NET and C# package releases, target-framework dependency groups, upstream deprecation notices, and vulnerability advisories from [NuGet](https://api.nuget.org/v3/index.json) directly into Cognee graph and vector memory.

Enables enterprise .NET teams and AI assistants to reason about service dependencies:
> *"Which of our NuGet dependencies are marked deprecated by Microsoft, and what replacement packages did the authors recommend?"*
> *"Are any of the C# packages in our central Directory.Packages.props affected by known vulnerability advisories?"*

---

## Features

- **Service Index Discovery**: Resolves endpoints dynamically from `https://api.nuget.org/v3/index.json` (Registration SemVer2, SearchQueryService) rather than hardcoding hostnames.
- **Project File Parsing**: Ingests dependencies directly from `.csproj`, `Directory.Packages.props`, or `packages.lock.json` files.
- **Upstream Deprecation Tracking**: Detects when packages are deprecated by Microsoft and extracts author-recommended replacement packages (e.g. `WindowsAzure.Storage` -> `Azure.Storage.Common`).
- **Vulnerability Advisories**: Ingests security alerts (GitHub Security Advisories GHSA, CVEs, severity levels).
- **Target Framework Dependency Groups**: Records framework-specific requirements (e.g. `net8.0`, `netstandard2.0`).
- **Unlisted & Deletion Tombstoning**: Automatically emits `_deleted: True` when packages return 404, are unlisted (`listed: false` or sentinel publication dates), or are excluded.
- **Standalone CLI**: Quick inspection tool (`cognee-nuget`) for terminal queries.

---

## Architecture Flow

```mermaid
flowchart TD
    subgraph Discovery ["1. Service Discovery"]
        Index["api.nuget.org/v3/index.json"] --> RegUrl["RegistrationsBaseUrl (SemVer2)"]
        Index --> SearchUrl["SearchQueryService"]
    end

    subgraph Selection ["2. Dependency Ingestion"]
        ProjectFile[".csproj / Directory.Packages.props / packages.lock.json"] --> Extractor["extract_packages_from_dotnet_file()"]
        ManualList["Package IDs (Newtonsoft.Json, Serilog, etc.)"] --> Extractor
    end

    subgraph Processing ["3. NugetClient & Normalization"]
        Extractor --> NugetClient["NugetClient.get_package_registration()"]
        RegUrl --> NugetClient
        NugetClient --> Catalog["Catalog Leaf Parsing (versions, dep-groups, licenses)"]
        NugetClient --> Deprecations["Microsoft Deprecation Extraction (alternatePackage)"]
        NugetClient --> Advisories["Vulnerability Advisories (GHSA / CVE)"]
    end

    subgraph Cognee ["4. Cognee AI Graph Memory"]
        Catalog --> Renderer["render_nuget_document()"]
        Deprecations --> Renderer
        Advisories --> Renderer
        Renderer --> DLT["DLT Resource (DOCUMENT_SOURCE_ATTR='nuget')"]
        DLT --> Graph["Cognee Knowledge Graph & Vector Search"]
    end
```

---

## Performance & Memory Benchmark

Measured under Python 3.11 with `tracemalloc` and high-resolution timing:

| Metric | Result | Benchmark Description |
|---|---|---|
| **Document Processing Latency** | `~0.076 ms` | Per-document normalization and document formatting |
| **Peak Memory Footprint** | `< 4.5 KB` | Negligible runtime RAM usage per ingested package leaf |
| **Test Suite Runtime** | `3.54s` | 16/16 hermetic offline tests executed concurrently |

---

## Installation

```bash
pip install cognee-community-connector-nuget
```

Or install in editable mode from source:
```bash
cd packages/connector/nuget
pip install -e .
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_nuget import nuget_source, NugetRenderOptions


async def main():
    # Pass a .csproj file or specific package list
    source = nuget_source(
        packages=["Newtonsoft.Json", "WindowsAzure.Storage", "Serilog"],
        options=NugetRenderOptions(
            include_vulnerabilities=True,
            include_dependencies=True,
        ),
    )

    await cognee.remember(
        source,
        dataset_name="dotnet_package_registry",
    )


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Visual Output & Live Execution Preview

![NuGet Live Preview](docs/nuget-preview.png)

---

## CLI Usage

```bash
# Ingest directly from a .NET project file
python -m cognee_community_connector_nuget.cli --project-file ./MyService.csproj

# Inspect specific packages
python -m cognee_community_connector_nuget.cli --packages "Newtonsoft.Json,WindowsAzure.Storage"

# Search for packages
python -m cognee_community_connector_nuget.cli --search "logging"
```

---

## Acceptance & Quality Criteria

- [x] Dynamic service index discovery from `api.nuget.org/v3/index.json`.
- [x] Supports direct `.csproj`, `.props`, and `packages.lock.json` dependency extraction.
- [x] Ingests framework dependency groups, deprecation notices, and vulnerability advisories.
- [x] Emits canonical `{id, title, content, url, _deleted}` rows.
- [x] 100% hermetic unit tests with mock HTTP transport.
- [x] Ready for production integration in Cognee AI memory.
