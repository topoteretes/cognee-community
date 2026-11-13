"""In-memory mock client simulating JSR REST API."""

from typing import Any

from cognee_community_connector_jsr.jsr import (
    JSRAPIError,
    JSRClient,
    JSRNotFoundError,
    JSRRateLimitedError,
)


class FakeJSRClient(JSRClient):
    """In-memory JSR client simulating real API endpoints with zero network calls."""

    def __init__(self, base_url: str = "https://api.jsr.io"):
        super().__init__(base_url=base_url, sleep=lambda _: None)
        self.scopes: dict[str, dict[str, dict[str, Any]]] = {}
        self.rate_limited = False
        self.server_error = False
        self.requests_count = 0
        self.setup_default_fixtures()

    def setup_default_fixtures(self) -> None:
        """Seed realistic JSR standard library packages."""
        self.scopes["std"] = {
            "path": {
                "scope": "std",
                "name": "path",
                "description": "Path manipulation library for universal TypeScript runtimes",
                "latestVersion": "1.0.8",
                "githubRepository": {"owner": "denoland", "name": "std"},
                "runtimeCompat": {"deno": True, "node": True, "browser": True},
                "updatedAt": "2026-10-15T12:00:00Z",
                "versions": {
                    "1.0.8": {
                        "version": "1.0.8",
                        "exports": {
                            ".": "./mod.ts",
                            "./posix": "./posix.ts",
                            "./windows": "./windows.ts",
                        },
                        "readme": (
                            "# Standard Path Utilities\nProvides cross-platform path resolution."
                        ),
                    }
                },
            },
            "fs": {
                "scope": "std",
                "name": "fs",
                "description": "Filesystem helper utilities",
                "latestVersion": "1.0.5",
                "githubRepository": {"owner": "denoland", "name": "std"},
                "runtimeCompat": {"deno": True, "node": True, "browser": False},
                "updatedAt": "2026-10-10T10:00:00Z",
                "versions": {
                    "1.0.5": {
                        "version": "1.0.5",
                        "exports": {".": "./mod.ts", "./copy": "./copy.ts"},
                        "readme": (
                            "# Standard Filesystem Utilities\n"
                            "Provides file copy, move, and walk functions."
                        ),
                    }
                },
            },
        }

    def list_scope_packages(self, scope: str) -> list[dict[str, Any]]:
        self.requests_count += 1
        if self.rate_limited:
            raise JSRRateLimitedError("JSR rate limit exceeded (HTTP 429)")
        if self.server_error:
            raise JSRAPIError("JSR request failed with HTTP 500: /scopes/{param}")

        if scope not in self.scopes:
            raise JSRNotFoundError(f"JSR scope not found: @{scope}")

        return [
            {
                "scope": pkg["scope"],
                "name": pkg["name"],
                "description": pkg["description"],
                "latestVersion": pkg["latestVersion"],
                "updatedAt": pkg["updatedAt"],
            }
            for pkg in self.scopes[scope].values()
        ]

    def get_package(self, scope: str, name: str) -> dict[str, Any]:
        self.requests_count += 1
        if self.rate_limited:
            raise JSRRateLimitedError("JSR rate limit exceeded (HTTP 429)")
        if self.server_error:
            raise JSRAPIError("JSR request failed with HTTP 500: /scopes/{param}")

        if scope not in self.scopes or name not in self.scopes[scope]:
            raise JSRNotFoundError(f"JSR package not found: @{scope}/{name}")

        pkg = self.scopes[scope][name]
        return {
            "scope": pkg["scope"],
            "name": pkg["name"],
            "description": pkg["description"],
            "latestVersion": pkg["latestVersion"],
            "githubRepository": pkg.get("githubRepository"),
            "runtimeCompat": pkg.get("runtimeCompat", {}),
            "updatedAt": pkg.get("updatedAt", ""),
        }

    def get_package_version(self, scope: str, name: str, version: str) -> dict[str, Any]:
        self.requests_count += 1
        if self.rate_limited:
            raise JSRRateLimitedError("JSR rate limit exceeded (HTTP 429)")
        if self.server_error:
            raise JSRAPIError("JSR request failed with HTTP 500: /scopes/{param}")

        self.get_package(scope, name)
        versions = self.scopes[scope][name].get("versions", {})
        if version not in versions:
            raise JSRNotFoundError(f"Version {version} not found for @{scope}/{name}")

        ver_info = versions[version]
        return {
            "version": version,
            "exports": ver_info.get("exports", {}),
            "readme": ver_info.get("readme", ""),
        }
