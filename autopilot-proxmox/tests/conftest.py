"""Shared pytest config.

Registers the ``integration`` marker (live-box tests) and a
``--run-integration`` CLI flag that gates their execution.
Default pytest runs skip integration tests so they never hit the box
unintentionally.
"""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.integration against a live autopilot host.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: test that hits a live autopilot web UI "
        "(see tests/integration/, skipped unless --run-integration is passed).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="need --run-integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
