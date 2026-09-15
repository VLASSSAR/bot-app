import json

from ..hh import SourceError, salary_text
from .common import Adapter, company_key, document, make, safe_url


class Getmatch(Adapter):
    name, url = "Getmatch", "https://getmatch.ru/vacancies"

    async def collect(self):
        # The service may inject promoted offers beyond limit. Increment by meta.limit.
        seen_pages = set()
        offset = 0
        for _ in range(50):
            r = await self.fetcher.get(
                "https://getmatch.ru/api/offers", params={"offset": offset, "limit": 100}
            )
            data = r.json()
            items = data["offers"]
            signature = tuple(v["id"] for v in items)
            if signature in seen_pages and items:
                raise SourceError("Getmatch повторяет страницу; сбор неполный.")
            seen_pages.add(signature)
            for item in items:
                key = company_key((item.get("company") or {}).get("name", ""))
                if not key or not item.get("is_active") or item.get("incognito_publication"):
                    continue
                locations = item.get("location_requirements") or []
                included = [loc for loc in locations if not loc.get("exclude")]
                # Explicit exclusion of Russia takes precedence over worldwide remote.
                excluded_ru = any(
                    loc.get("exclude") and loc.get("location_id") == "russia" for loc in locations
                )
                ru = [
                    loc
                    for loc in included
                    if loc.get("country") == "Россия"
                    or "russia" in loc.get("ancestors", [])
                    or loc.get("location_id") == "russia"
                ]
                worldwide = [
                    loc
                    for loc in included
                    if loc.get("location_id") == "_cu-world" and loc.get("format") == "remote"
                ]
                if excluded_ru or (included and not ru and not worldwide):
                    continue
                suitable = ru or worldwide
                remote = any(loc.get("format") == "remote" for loc in suitable)
                fmt = {"remote": "Удалённо", "office": "Офис", "hybrid": "Гибрид"}
                salary = ""
                if not item.get("salary_hidden") and not item.get("salary_by_our_version"):
                    salary = salary_text(
                        {
                            "from": item.get("salary_display_from"),
                            "to": item.get("salary_display_to"),
                            "currency": item.get("salary_currency"),
                            "gross": {"net": False, "gross": True}.get(item.get("salary_taxes")),
                        }
                    )
                self.add(
                    make(
                        self.name,
                        key,
                        item["id"],
                        item["position"],
                        safe_url(self.url, item["url"]),
                        city=", ".join(dict.fromkeys(loc.get("city", "") for loc in suitable)),
                        work=", ".join(
                            dict.fromkeys(fmt.get(loc.get("format"), "") for loc in suitable)
                        ),
                        remote=remote,
                        region=bool(ru or worldwide),
                        salary=salary,
                        published=item.get("published_at"),
                        priority=1,
                    )
                )
            meta = data["meta"]
            offset += int(meta["limit"])
            if offset >= int(meta["total"]):
                break
            if not items or int(meta["limit"]) <= 0:
                raise SourceError("Неполная выдача Getmatch.")
        else:
            self.partial("Достигнут лимит 50 страниц Getmatch.")


class Habr(Adapter):
    name, url = "Хабр Карьера", "https://career.habr.com/vacancies"

    async def collect(self):
        for query in ("продукт", "продакт", "проект", "product", "project"):
            seen = set()
            for page in range(1, 31):
                r = await self.fetcher.get(
                    self.url, params={"q": query, "page": page, "type": "all"}
                )
                d = document(r.text)
                states = [
                    json.loads(s) for s in d.xpath('//script[@type="application/json"]/text()')
                ]
                state = next((x for x in states if "vacancies" in x), None)
                if state is None:
                    raise SourceError("Не найдены данные вакансий Хабр Карьеры.")
                items = state["vacancies"]["list"] or []
                ids = {v["id"] for v in items}
                if items and ids <= seen:
                    raise SourceError("Хабр Карьера повторяет страницу; сбор неполный.")
                seen.update(ids)
                for item in items:
                    key = company_key(item["company"]["title"])
                    if not key or item.get("archived") or item.get("hidden"):
                        continue
                    self.add(
                        make(
                            self.name,
                            key,
                            item["id"],
                            item["title"],
                            safe_url(self.url, item["href"]),
                            city=", ".join(v["title"] for v in (item.get("locations") or [])),
                            work="Удалённо" if item.get("remoteWork") else "",
                            remote=item.get("remoteWork", False),
                            salary=(item.get("salary") or {}).get("formatted", ""),
                            experience=item.get("qualification", ""),
                            published=(item.get("publishedDate") or {}).get("date"),
                            priority=1,
                        )
                    )
                # Follow the rendered pagination, never the unrelated predicted salary.
                next_links = d.xpath('//a[@rel="next"]/@href')
                pagination = state["vacancies"].get("pagination") or {}
                total_pages = pagination.get("totalPages") or pagination.get("total_pages")
                if not items or (total_pages and page >= total_pages):
                    break
                if not next_links and not d.xpath(f'//a[contains(@href,"page={page + 1}")]'):
                    break
            else:
                self.partial("Достигнут лимит 30 страниц на поисковый запрос Хабр Карьеры.")
