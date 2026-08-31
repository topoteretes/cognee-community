# Cognee Community Hybrid Adapter — Turso/libSQL

> Initial scaffold for Issue #125.

This package will provide a Turso/libSQL backend for both graph and vector
operations in Cognee.

## Planned architecture

- **Graph:** nodes and edges represented in libSQL tables.
- **Vector:** embeddings stored using libSQL vector capabilities.
- **Hybrid:** one backend registered for both graph and vector operations.
- **Embedded mode:** local `file:` database URIs.
- **Remote mode:** future support for libSQL servers and Turso Cloud.
- **Dataset isolation:** potentially one database file/database per dataset.

## Current status

The package structure and public import surface are scaffolded.

The concrete implementation of Cognee's `GraphDBInterface` and
`VectorDBInterface`, registration hooks, tests, and examples are still
in progress.

## Development

This package is being implemented for:

- `topoteretes/cognee-community#125`
