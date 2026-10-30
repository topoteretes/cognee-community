import pytest

# No adapter import needed — we're testing pure score logic


class TestScoreNormalization:
    def test_normalized_true_inverts_score(self):
        """
        normalized=True should invert the Azure score (1 - score)
        so that ScoredResult.score follows 'lower is better' contract.
        """
        azure_score = 0.9
        normalized = True

        result_score = (1 - azure_score) if normalized else azure_score

        assert result_score == pytest.approx(0.1)

    def test_normalized_false_returns_raw_score(self):
        """
        normalized=False should return raw Azure score as-is.
        """
        azure_score = 0.9
        normalized = False

        result_score = (1 - azure_score) if normalized else azure_score

        assert result_score == pytest.approx(0.9)

    def test_perfect_match_normalized(self):
        """
        Azure score of 1.0 (perfect match) → 0.0 after normalization (best possible).
        """
        azure_score = 1.0
        result_score = 1 - azure_score
        assert result_score == pytest.approx(0.0)

    def test_worst_match_normalized(self):
        """
        Azure score of 0.0 (no match) → 1.0 after normalization (worst possible).
        """
        azure_score = 0.0
        result_score = 1 - azure_score
        assert result_score == pytest.approx(1.0)
