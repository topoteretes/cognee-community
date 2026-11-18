"""Opt-in for real memory acceptance on an approved isolated runner."""


def pytest_addoption(parser):
    parser.addoption(
        "--run-jenkins-memory",
        action="store_true",
        default=False,
        help="Run real memory acceptance in a fresh disposable process (approved runner only)",
    )
