# Chess.com Connector for Cognee

Sync your Chess.com games into Cognee memory.

No API key needed — public API only. Set CHESSCOM_USERNAME (your Chess.com username).

Install: uv pip install -e ./packages/connector/chesscom (from repo root /home/one-piece/cognee2/cognee-community)
Run: python packages/connector/chesscom/examples/example.py

Row shape: {"id": "chesscom:<uuid>", "url": "...", "title": "Rapid vs opponent (win)", "content": markdown with result, ratings, opening, accuracy, PGN}.
Tests: pytest packages/connector/chesscom/tests/ -v
