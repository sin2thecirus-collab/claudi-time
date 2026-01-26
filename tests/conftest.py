"""Test-Konfiguration und Fixtures."""

import pytest
from httpx import AsyncClient

from app.main import app


@pytest.fixture
def anyio_backend():
    """Async Backend für Tests."""
    return "asyncio"


@pytest.fixture
async def client():
    """Async Test-Client."""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac
