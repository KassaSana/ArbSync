import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-wire-fixtures",
        action="store_true",
        default=False,
        help="rewrite server/tests/fixtures/wire/*.json from what the backend emits",
    )
