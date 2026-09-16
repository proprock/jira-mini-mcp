"""Bootstrap smoke test: every module imports and the entry point exists."""

import importlib


def test_package_modules_import() -> None:
    for module_name in (
        "jira_mini_mcp",
        "jira_mini_mcp.server",
        "jira_mini_mcp.jira",
        "jira_mini_mcp.auth",
        "jira_mini_mcp.models",
        "jira_mini_mcp.errors",
    ):
        module = importlib.import_module(module_name)
        assert module is not None


def test_server_main_is_callable() -> None:
    from jira_mini_mcp.server import main

    assert callable(main)
