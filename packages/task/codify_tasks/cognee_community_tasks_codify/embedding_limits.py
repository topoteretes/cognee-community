"""Keep code data points within the embedding model's per-input token limit.

Embedding providers cap each input item (e.g. Azure/OpenAI text-embedding
models reject inputs above 8192 tokens with a 400, which aborts the whole
add_data_points batch after retries). Code data points carry raw source in
their embeddable ``source_code`` field — a single large class or file easily
exceeds the cap — so anything yielded to the pipeline gets trimmed here.
"""

from __future__ import annotations

from cognee.shared.logging_utils import get_logger

logger = get_logger("codify_embedding_limits")

# Azure/OpenAI text-embedding models accept at most 8192 tokens per input;
# keep a small margin since provider-side token counts can differ slightly
# from the local tokenizer's.
DEFAULT_TOKEN_BUDGET = 8000

TRUNCATION_MARKER = "\n# ... [source truncated to fit the embedding token limit]"


def _get_tokenizer_and_budget():
    """Return (tokenizer, token_budget) from the configured embedding engine.

    Falls back to (None, DEFAULT_TOKEN_BUDGET) when the engine or tokenizer
    is unavailable (e.g. in offline unit tests).
    """
    try:
        from cognee.infrastructure.databases.vector.embeddings import get_embedding_engine

        engine = get_embedding_engine()
        tokenizer = getattr(engine, "tokenizer", None)
        budget = getattr(engine, "max_completion_tokens", None) or 0
        budget = min(budget, DEFAULT_TOKEN_BUDGET) if budget > 0 else DEFAULT_TOKEN_BUDGET
        return tokenizer, budget
    except Exception:
        return None, DEFAULT_TOKEN_BUDGET


def trim_text_to_token_budget(text, tokenizer=None, budget: int | None = None) -> str | None:
    """Trim *text* so it embeds within *budget* tokens.

    Uses the embedding engine's tokenizer to measure when available;
    otherwise falls back to a conservative characters-per-token estimate
    (code tokenizes densely, so ~3 chars/token).
    """
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")

    if budget is None or tokenizer is None:
        default_tokenizer, default_budget = _get_tokenizer_and_budget()
        tokenizer = tokenizer or default_tokenizer
        budget = budget or default_budget

    max_chars_fallback = budget * 3
    if tokenizer is None:
        if len(text) <= max_chars_fallback:
            return text
        return text[:max_chars_fallback] + TRUNCATION_MARKER

    try:
        if tokenizer.count_tokens(text) <= budget:
            return text

        # Cut proportionally and re-measure until the text fits; converges in
        # a handful of iterations.
        candidate = text
        while len(candidate) > 0:
            token_count = tokenizer.count_tokens(candidate)
            if token_count <= budget:
                break
            keep_ratio = budget / token_count
            new_length = max(1, int(len(candidate) * keep_ratio * 0.98))
            if new_length >= len(candidate):
                new_length = len(candidate) - 1
            candidate = candidate[:new_length]
        return candidate + TRUNCATION_MARKER
    except Exception as error:
        logger.warning("Tokenizer-based trim failed (%s); using character fallback.", error)
        if len(text) <= max_chars_fallback:
            return text
        return text[:max_chars_fallback] + TRUNCATION_MARKER


def enforce_embedding_limits(code_file) -> None:
    """Trim the embeddable source_code fields of a CodeFile and its parts in place."""
    tokenizer, budget = _get_tokenizer_and_budget()

    def _trim_attr(data_point) -> None:
        source = getattr(data_point, "source_code", None)
        if source is None:
            return
        trimmed = trim_text_to_token_budget(source, tokenizer=tokenizer, budget=budget)
        if trimmed is not source:
            data_point.source_code = trimmed
            if isinstance(trimmed, str) and trimmed.endswith(TRUNCATION_MARKER):
                logger.info(
                    "Trimmed oversized source for embedding: %s",
                    getattr(data_point, "file_path", None) or getattr(data_point, "name", "?"),
                )

    _trim_attr(code_file)
    for attribute_name in (
        "provides_function_definition",
        "provides_class_definition",
        "depends_on",
    ):
        for part in getattr(code_file, attribute_name, None) or []:
            _trim_attr(part)
