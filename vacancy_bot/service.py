import asyncio
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from .config import COMPANIES
from .hh import HHClient, SourceError
from .models import Collection, SourceStatus
from .sources.boards import Getmatch, Habr
from .sources.common import Fetcher
from .sources.employers import MTS, VK, Avito, Ozon, Sber, TBank, Wildberries, Yandex

ADAPTERS = (Yandex, VK, Sber, TBank, Ozon, Wildberries, Avito, MTS, Getmatch, Habr)


def deduplicate(vacancies):
    def norm(value):
        return re.sub(r"\W+", " ", value.casefold().replace("ё", "е")).strip()

    groups = {}
    source_counts = {}
    for v in {v.url: v for v in vacancies}.values():
        key = (norm(v.company), norm(v.title), norm(v.city), v.source)
        source_counts[key] = source_counts.get(key, 0) + 1
    ambiguous = {key[:3] for key, count in source_counts.items() if count > 1}
    for v in sorted(vacancies, key=lambda x: x.priority):
        # No fuzzy matching. Multiple distinct same-site job IDs remain separate.
        key = (norm(v.company), norm(v.title), norm(v.city))
        candidates = groups.setdefault(key, [])
        existing = next((old for old in candidates if old.url == v.url), None)
        if existing:
            continue
        existing = next(
            (
                old
                for old in candidates
                if old.source != v.source and old.city != "Не указан" and key not in ambiguous
            ),
            None,
        )
        if existing:
            i = candidates.index(existing)
            candidates[i] = replace(
                existing,
                alternate_urls=tuple(
                    dict.fromkeys((*existing.alternate_urls, v.url, *v.alternate_urls))
                ),
            )
        else:
            candidates.append(v)
    return [v for group in groups.values() for v in group]


class Service:
    def __init__(self, settings, storage):
        self.settings, self.storage = settings, storage
        self.lock = asyncio.Lock()
        self.cache = None
        self.cached_at = 0
        self.cache_companies = None

    async def collect(self, filters, force=False):
        async with self.lock:
            # One bounded source crawl shared by simultaneous users for 15 minutes.
            if (
                force
                or self.cache is None
                or time.monotonic() - self.cached_at > 900
                or self.cache_companies != filters.companies
            ):
                async with httpx.AsyncClient(
                    timeout=30,
                    follow_redirects=True,
                    headers={"User-Agent": self.settings.user_agent},
                ) as client:
                    fetcher = Fetcher(client)
                    adapters = [
                        a(fetcher) for a in ADAPTERS if a.key is None or a.key in filters.companies
                    ]
                    semaphore = asyncio.Semaphore(3)

                    async def run(adapter):
                        async with semaphore:
                            try:
                                return await asyncio.wait_for(adapter.run(), timeout=240)
                            except TimeoutError:
                                return adapter.items, SourceStatus(
                                    adapter.name,
                                    adapter.url,
                                    "partial" if adapter.items else "error",
                                    len(adapter.items),
                                    "Превышено время сбора.",
                                )

                    results = await asyncio.gather(*(run(a) for a in adapters))
                now = datetime.now(UTC)
                vacancies = [v for items, _ in results for v in items]
                vacancies = self.storage.observe(vacancies, now)
                self.cache = (vacancies, [status for _, status in results], now)
                self.cached_at = time.monotonic()
                self.cache_companies = filters.companies
            vacancies, statuses, now = self.cache
            vacancies, statuses = list(vacancies), list(statuses)
            if filters.include_hh:
                headers = {"User-Agent": self.settings.user_agent}
                if self.settings.hh_token:
                    headers["Authorization"] = f"Bearer {self.settings.hh_token}"
                async with httpx.AsyncClient(
                    base_url="https://api.hh.ru", headers=headers, timeout=25
                ) as client:
                    try:
                        hh = await asyncio.wait_for(HHClient(client).collect(filters), timeout=240)
                        vacancies.extend(
                            self.storage.observe(
                                [
                                    replace(
                                        v,
                                        region_confirmed=True,
                                        remote="удал" in v.work_format.lower(),
                                    )
                                    for v in hh.vacancies
                                ],
                                now,
                            )
                        )
                        statuses.append(
                            SourceStatus(
                                "hh.ru",
                                "https://hh.ru",
                                "partial" if hh.warnings else "ok",
                                len(hh.vacancies),
                                "; ".join(hh.warnings),
                            )
                        )
                    except (SourceError, TimeoutError) as exc:
                        statuses.append(
                            SourceStatus(
                                "hh.ru",
                                "https://hh.ru",
                                "error",
                                0,
                                str(exc) or "Превышено время сбора.",
                            )
                        )
            if not any(s.state in ("ok", "partial") for s in statuses):
                raise SourceError("Источники недоступны; отчет не сформирован. Попробуйте позже.")
            cutoff = datetime.now(UTC) - timedelta(days=filters.days)
            allowed = {COMPANIES[k][0] for k in filters.companies}
            filtered = [
                v
                for v in vacancies
                if v.company in allowed
                and (not filters.remote_only or v.remote)
                and (v.published_at or v.first_seen) >= cutoff
                and (v.published_at or v.first_seen) <= datetime.now(UTC)
            ]
            filtered = deduplicate(filtered)
            filtered.sort(key=lambda v: (v.priority, -(v.published_at or v.first_seen).timestamp()))
            return Collection(filtered, filters, now, sources=statuses)
