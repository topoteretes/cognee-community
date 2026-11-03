# Cognee Community Connector: Trello

A community data-source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests **Trello** boards, lists, cards, checklists, and comment threads into the Cognee graph and vector memory.

Resolves: [`cognee#6471`](https://github.com/topoteretes/cognee/issues/6471).

---

## Features

- **Rich Card Documents:** Ingests each card with its stage/list, labels, due date status, checklists (with item states), and full comment discussion threads.
- **Incremental Cursor Sync:** Tracks `dateLastActivity` as a cursor to only sync modified cards on subsequent runs.
- **Forget-On-Delete Tombstones:** Identifies cards that have been removed or archived from the board and yields `_deleted: True` records so Cognee can prune memory cleanly.
- **Cognify Ready:** Formats clean Markdown documents structured for semantic search and Q&A (e.g. *"What did we decide about X on that card?"*, *"Which cards are overdue?"*).
- **Environment & Board Auto-Discovery:** Supports `TRELLO_API_KEY`, `TRELLO_TOKEN`, and `TRELLO_BOARD_IDS` or programmatic parameters.

---

## Installation

```bash
pip install cognee-community-connector-trello
```

---

## Usage

```python
import cognee
from cognee_community_connector_trello import trello_source

# Sync specific boards or auto-discover from user's account
cognee.add(trello_source(board_ids=["60d5ec49e49759223a4f131a"]))
cognee.cognify()

# Ask questions about project boards
results = cognee.search("Which cards in the launch list are currently overdue?")
```
