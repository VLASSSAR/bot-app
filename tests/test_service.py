from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from vacancy_bot.config import Settings
from vacancy_bot.hh import SourceError
from vacancy_bot.models import Filters, SourceStatus
from vacancy_bot.service import Service
from vacancy_bot.sources.common import make
from vacancy_bot.storage import Storage


async def test_cache_period_remote_and_employer_filters(tmp_path, monkeypatch):
    calls = []
    now = datetime.now(UTC)

    class Fake:
        key = None
        name = "Source"
        url = "https://example.com"
        items = []

        def __init__(self, fetcher):
            pass

        async def run(self):
            calls.append(1)
            a = make(
                "Source",
                "yandex",
                "1",
                "Product Manager",
                "https://example.com/1",
                city="Москва",
                remote=True,
                published=now.isoformat(),
            )
            old = replace(
                a, id="2", url="https://example.com/2", published_at=now - timedelta(days=10)
            )
            b = replace(a, id="3", url="https://example.com/3", company="VK", remote=False)
            return [a, old, b], SourceStatus(self.name, self.url, count=3)

    monkeypatch.setattr("vacancy_bot.service.ADAPTERS", (Fake,))
    cfg = Settings("", frozenset(), "test", tmp_path / "db", ZoneInfo("UTC"))
    service = Service(cfg, Storage(cfg.database_path))
    result = await service.collect(Filters(days=7, remote_only=True, companies=("yandex",)))
    assert len(result.vacancies) == 1 and result.vacancies[0].id == "1"
    result = await service.collect(Filters(days=30, companies=("yandex",)))
    assert len(calls) == 1 and len(result.vacancies) == 2
    assert result.vacancies[0].first_seen is not None


async def test_all_failed_sources_raise(tmp_path, monkeypatch):
    class Failed:
        key = None

        def __init__(self, fetcher):
            pass

        async def run(self):
            return [], SourceStatus("Failed", "https://example.com", "error")

    monkeypatch.setattr("vacancy_bot.service.ADAPTERS", (Failed,))
    cfg = Settings("", frozenset(), "test", tmp_path / "db", ZoneInfo("UTC"))
    with pytest.raises(SourceError, match="недоступны"):
        await Service(cfg, Storage(cfg.database_path)).collect(Filters())
