"""Shared pytest fixtures.

`anyio` ships as a transitive dependency of `mcp` and registers its own
pytest plugin, so async tests use `@pytest.mark.anyio` instead of adding
`pytest-asyncio` as a separate dev dependency.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
