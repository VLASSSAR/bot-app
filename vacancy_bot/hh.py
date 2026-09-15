import asyncio
import html
import re
from datetime import UTC, datetime, timedelta

import httpx

from .config import COMPANIES
from .matching import classify
from .models import Collection, Filters, Vacancy

# Separate broad title queries keep the API language simple and improve recall.
QUERIES = ("product", "продукт", "продакт", "project", "проект", "проджект")


class SourceError(Exception):
    """Safe, user-visible source error without tokens or raw response bodies."""


def clean(value: str | None) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", "", value or ""))
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value).strip()


def salary_text(salary: dict | None) -> str:
    if not salary or (salary.get("from") is None and salary.get("to") is None):
        return "Не указана"
    parts = []
    for key, label in (("from", "от"), ("to", "до")):
        if salary.get(key) is not None:
            parts.append(f"{label} {salary[key]:,}".replace(",", " "))
    currency = {"RUR": "₽", "RUB": "₽"}.get(salary.get("currency"), salary.get("currency", ""))
    tax = {True: "до вычета налогов", False: "на руки"}.get(salary.get("gross"), "")
    return " ".join(parts + [currency, tax]).strip()


def normalize(item: dict, company: str) -> Vacancy | None:
    role = classify(item["name"])
    if role is None or item.get("archived"):
        return None
    formats = item.get("work_format") or []
    work = ", ".join(clean(x.get("name")) for x in formats if x.get("name"))
    if not work and (item.get("schedule") or {}).get("id") == "remote":
        work = "Удалённо"
    snippet = item.get("snippet") or {}
    # Canonical numeric HH URLs cannot inject external or unsafe hyperlinks.
    vid = str(item["id"])
    if not vid.isdigit():
        raise ValueError("Invalid vacancy ID")
    published = datetime.fromisoformat(item["published_at"])
    if published.tzinfo is None:
        raise ValueError("Missing publication timezone")
    return Vacancy(
        vid,
        clean(item["name"]),
        company,
        role,
        clean((item.get("area") or {}).get("name")) or "Не указан",
        work or "Не указан",
        salary_text(item.get("salary")),
        clean((item.get("experience") or {}).get("name")) or "Не указан",
        published,
        f"https://hh.ru/vacancy/{vid}",
        clean(snippet.get("requirement")),
        clean(snippet.get("responsibility")),
    )


class HHClient:
    def __init__(self, client: httpx.AsyncClient, pause: float = 0.25, max_pages: int = 20):
        self.client = client
        self.pause = pause
        self.max_pages = max_pages

    async def request(self, params: dict) -> dict:
        for attempt in range(3):
            try:
                await asyncio.sleep(self.pause)
                response = await self.client.get("/vacancies", params=params)
                if response.status_code in (401, 403):
                    raise SourceError(
                        "hh.ru отказал в доступе (401/403). Проверьте доступ "
                        "к API с сервера, User-Agent и HH_ACCESS_TOKEN."
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        try:
                            delay = float(response.headers.get("Retry-After", 2**attempt))
                        except ValueError:
                            delay = 2**attempt
                        await asyncio.sleep(min(max(delay, 0), 30))
                        continue
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise ValueError("Invalid response schema")
                return data
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 2:
                    raise SourceError(
                        "Не удалось получить ответ hh.ru после трёх попыток."
                    ) from exc
                await asyncio.sleep(2**attempt)
        raise SourceError("hh.ru временно недоступен или ограничил частоту запросов.")

    async def collect(self, filters: Filters) -> Collection:
        now = datetime.now(UTC)
        result = Collection([], filters, now, total_searches=len(filters.companies) * len(QUERIES))
        seen = set()
        for key in filters.companies:
            company, employer_ids = COMPANIES[key]
            for query in QUERIES:
                params = {
                    "text": query,
                    "search_field": "name",
                    "employer_id": employer_ids,
                    "area": "113",
                    "date_from": (now - timedelta(days=filters.days)).isoformat(),
                    "date_to": now.isoformat(),
                    "order_by": "publication_time",
                    "per_page": 100,
                }
                if filters.remote_only:
                    params["work_format"] = "REMOTE"
                try:
                    for page in range(self.max_pages):
                        data = await self.request({**params, "page": page})
                        for item in data["items"]:
                            try:
                                if str((item.get("employer") or {}).get("id")) not in employer_ids:
                                    continue
                                if filters.remote_only:
                                    remote = any(
                                        x.get("id") == "REMOTE"
                                        for x in item.get("work_format") or []
                                    )
                                    if (
                                        not remote
                                        and (item.get("schedule") or {}).get("id") != "remote"
                                    ):
                                        continue
                                vacancy = normalize(item, company)
                                if (
                                    vacancy
                                    and now - timedelta(days=filters.days)
                                    <= vacancy.published_at
                                    <= now
                                ):
                                    if vacancy.id not in seen:
                                        result.vacancies.append(vacancy)
                                        seen.add(vacancy.id)
                            except (KeyError, TypeError, ValueError):
                                note = f"{company}: пропущена запись с некорректными данными."
                                if note not in result.warnings:
                                    result.warnings.append(note)
                        pages = max(1, int(data.get("pages", 1)))
                        if page + 1 >= pages:
                            break
                    if pages > self.max_pages or int(data.get("found", 0)) > 2000:
                        result.warnings.append(
                            f"{company}, «{query}»: достигнут лимит выдачи; "
                            "сократите период для более полного результата."
                        )
                    result.successful_searches += 1
                except SourceError as exc:
                    # Access denial is usually global; do not hammer all remaining searches.
                    if "401/403" in str(exc):
                        if result.successful_searches == 0:
                            raise
                        result.warnings.append(str(exc) + " Сбор прерван; отчет неполный.")
                        result.vacancies.sort(key=lambda v: v.published_at, reverse=True)
                        return result
                    result.warnings.append(f"{company}, «{query}»: {exc}")
        if result.successful_searches == 0:
            raise SourceError("Сбор не выполнен: hh.ru недоступен. Попробуйте позже.")
        result.vacancies.sort(key=lambda v: v.published_at, reverse=True)
        return result
