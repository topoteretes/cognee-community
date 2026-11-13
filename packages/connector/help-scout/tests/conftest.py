"""Network/LLM checks require an explicit command-line opt-in."""


def pytest_addoption(parser):
    parser.addoption(
        "--run-llm-tests",
        action="store_true",
        default=False,
        help="Allow tests that call a live LLM (requires LLM_API_KEY)",
    )
