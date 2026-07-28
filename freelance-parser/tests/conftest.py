from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from fparser.models import Order
from fparser.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def read_fixture():
    def _read(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    return _read


@pytest_asyncio.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    store = Storage(str(tmp_path / "test.db"))
    await store.open()
    try:
        yield store
    finally:
        await store.close()


def make_order(
    *,
    source: str = "kwork",
    native_id: str = "1",
    title: str = "Нужен лендинг",
    description: str = "",
    budget_min: int | None = None,
    budget_max: int | None = None,
    category: str | None = None,
    published_at: datetime | None = None,
    responses_count: int | None = None,
) -> Order:
    return Order(
        source=source,
        native_id=native_id,
        title=title,
        url=f"https://example.com/{source}/{native_id}",
        description=description,
        budget_min=budget_min,
        budget_max=budget_max,
        category=category,
        published_at=published_at or datetime(2026, 7, 28, 4, 0, tzinfo=UTC),
        responses_count=responses_count,
    )
