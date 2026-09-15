import re
from urllib.parse import parse_qs, urlparse

from ..hh import SourceError, salary_text
from ..matching import classify
from .common import Adapter, document, json_field, make, rsc_text, safe_url


class Yandex(Adapter):
    name, key, url = "Яндекс Карьера", "yandex", "https://yandex.ru/jobs/vacancies"

    async def collect(self):
        for profession in ("product-manager", "project-manager", "tech-manager"):
            params = {"profession": profession}
            cursors = set()
            for _ in range(30):
                r = await self.fetcher.get(self.url, params=params)
                text = rsc_text(r.text)
                batches = [
                    x
                    for x in json_field(text, "results")
                    if isinstance(x, list) and (not x or "publication_slug_url" in x[0])
                ]
                if not batches:
                    raise SourceError("Не найден список вакансий в данных страницы Яндекса.")
                for item in batches[0]:
                    v = item.get("vacancy") or {}
                    modes = v.get("work_modes") or []
                    self.add(
                        make(
                            self.name,
                            self.key,
                            item["id"],
                            item["title"],
                            safe_url(self.url, "/jobs/vacancies/" + item["publication_slug_url"]),
                            city=", ".join(c["name"] for c in v.get("cities", [])),
                            work=", ".join(m["name"] for m in modes),
                            remote=any(m.get("slug") == "remote" for m in modes),
                            summary=item.get("short_summary", ""),
                        )
                    )
                # 'modified' is NOT a publication date. Follow only the cursor on our public host.
                next_urls = [
                    x for x in json_field(text, "next") if isinstance(x, str) and "cursor=" in x
                ]
                if not next_urls:
                    break
                cursor = parse_qs(urlparse(next_urls[-1]).query).get("cursor", [None])[0]
                if not cursor or cursor in cursors:
                    self.partial("Повторился курсор; выдача может быть неполной.")
                    break
                cursors.add(cursor)
                params["cursor"] = cursor
            else:
                self.partial("Достигнут лимит 30 страниц на направление.")


class VK(Adapter):
    name, key, url = "VK Team", "vk", "https://team.vk.company/vacancy/"

    async def collect(self):
        offset = 0
        for _ in range(30):
            r = await self.fetcher.get(
                "https://team.vk.company/career/api/v2/vacancies/",
                params={"limit": 50, "offset": offset},
            )
            data = r.json()
            items = data["results"]
            for item in items:
                self.add(
                    make(
                        self.name,
                        self.key,
                        item["id"],
                        item["title"],
                        safe_url(self.url, f"/vacancy/{item['id']}/"),
                        city=(item.get("town") or {}).get("name", ""),
                        work=item.get("work_format", ""),
                        remote=item.get("remote", False),
                    )
                )
            offset += len(items)
            if offset >= data["count"]:
                break
            if not items:
                raise SourceError("Пустая страница до окончания списка VK.")
        else:
            self.partial("Достигнут лимит 1500 вакансий VK.")


class MTS(Adapter):
    name, key, url = "МТС Карьера", "mts", "https://job.mts.ru/vacancies"

    async def collect(self):
        seen = set()
        for page in range(1, 61):
            r = await self.fetcher.get(
                "https://job.mts.ru/api/v2/vacancies",
                params={"pagination[page]": page, "pagination[pageSize]": 100},
            )
            data = r.json()
            items = data["data"]
            ids = {str(v["slug"]) for v in items}
            if items and ids <= seen:
                raise SourceError("МТС повторяет страницу: сбор неполный.")
            seen.update(ids)
            for item in items:
                if not item.get("isActive", False):
                    continue
                work = ", ".join(v["title"] for v in item.get("workFormats", []))
                self.add(
                    make(
                        self.name,
                        self.key,
                        item["slug"],
                        item["title"],
                        safe_url(self.url, "/vacancy/" + str(item["slug"])),
                        city=(item.get("region") or {}).get("title", ""),
                        work=work,
                        experience=(item.get("experience") or {}).get("title", ""),
                        salary=salary_text(
                            {
                                "from": item.get("salaryFrom"),
                                "to": item.get("salaryTo"),
                                "currency": (item.get("currency") or {}).get("title"),
                                "gross": None,
                            }
                        ),
                        published=item.get("publishedAt"),
                        remote=bool(re.search("удал|дистанц", work, re.I)),
                    )
                )
            if page >= data["meta"]["pagination"]["pageCount"]:
                break
        else:
            self.partial("Достигнут лимит 60 страниц МТС.")


class Avito(Adapter):
    name, key, url = "Авито Карьера", "avito", "https://career.avito.com/vacancies/"

    async def collect(self):
        r = await self.fetcher.get(self.url)
        d = document(r.text)
        cards = d.xpath("//*[@data-vacancy-id]")
        if not cards:
            raise SourceError("На странице Авито не найдены карточки вакансий.")
        for card in cards:
            anchors = card.xpath('.//a[contains(@class,"vacancies-section__item-name")]')
            if not anchors:
                continue
            a = anchors[0]
            work = " ".join(card.xpath('.//*[contains(@class,"item-format")]/text()')).strip()
            self.add(
                make(
                    self.name,
                    self.key,
                    card.get("data-vacancy-id"),
                    a.text_content(),
                    safe_url(self.url, a.get("href")),
                    city=card.get("data-vacancy-geo", ""),
                    work=work,
                    remote=bool(re.search("удал|дистанц", work, re.I)),
                )
            )
        if d.xpath('//a[contains(@href,"PAGEN")]|//button[contains(@class,"load-more")]'):
            self.partial("Обнаружена дополнительная пагинация; загружена первая страница.")


class TBank(Adapter):
    name, key, url = "Т-Банк Карьера", "tbank", "https://www.tbank.ru/career/vacancies/it/"

    async def collect(self):
        offset = 0
        seen = set()
        for _ in range(30):
            r = await self.fetcher.request(
                "POST",
                "https://www.tbank.ru/pfpjobs/papi/getVacancies",
                json={
                    "filters": {
                        "generatedGraphQL": {
                            "type": "T_CAREER",
                            "categories": ["tcareer_it", "tcareer_back_office"],
                        }
                    },
                    "pagination": {"offset": offset},
                    "limit": 100,
                },
            )
            data = r.json()["payload"]
            items = data["vacancies"]
            ids = {item["urlSlug"] for item in items}
            if ids and ids <= seen:
                raise SourceError("Т-Банк повторяет страницу; сбор неполный.")
            seen.update(ids)
            for item in items:
                category = "it" if item["category"] == "tcareer_it" else "back-office"
                # The public route resolves the publication by UUID. City in the route
                # is navigation context; vacancy location comes from the source record.
                path = f"/career/{category}/vacancy/moscow/{item['seoSlug']}/{item['urlSlug']}/"
                tags = ", ".join(item.get("tags") or [])
                self.add(
                    make(
                        self.name,
                        self.key,
                        item["urlSlug"],
                        item["title"],
                        safe_url(self.url, path),
                        city=item.get("subtitle", ""),
                        work=", ".join(
                            t
                            for t in (item.get("tags") or [])
                            if re.search("офис|гибрид|удал|дистанц", t, re.I)
                        ),
                        experience=", ".join(
                            t
                            for t in (item.get("tags") or [])
                            if re.search("senior|middle|junior|lead|опыт|лет|года", t, re.I)
                        ),
                        summary=item.get("shortDescription", ""),
                        remote=bool(re.search("удал|дистанц", tags, re.I)),
                    )
                )
            pagination = data["nextPagination"]
            if pagination["isFinished"]:
                break
            if pagination["offset"] <= offset:
                raise SourceError("Т-Банк не продвинул пагинацию.")
            offset = pagination["offset"]
        else:
            self.partial("Достигнут лимит 30 страниц Т-Банка.")


class Ozon(Adapter):
    name, key, url = "Ozon Карьера", "ozon", "https://job.ozon.ru/vacancy/"

    async def collect(self):
        await self.fetcher.get(self.url)
        raise SourceError("Адаптер Ozon требует проверки: при разработке сайт ограничил доступ.")


class Wildberries(Adapter):
    name, key, url = "RWB Карьера", "wildberries", "https://career.rwb.ru/vacancies"

    async def collect(self):
        await self.fetcher.get(
            "https://career.rwb.ru/crm-api/api/v1/pub/vacancies", params={"limit": 100, "offset": 0}
        )
        raise SourceError("Адаптер RWB требует проверки: публичный API ограничил доступ.")


class Sber(Adapter):
    name, key, url = "Сбер Карьера", "sber", "https://rabota.sber.ru/search/"

    @staticmethod
    def slug(title):
        letters = dict(
            zip(
                "абвгдеёжзийклмнопрстуфхцчшщьыъэюя",
                [
                    "a",
                    "b",
                    "v",
                    "g",
                    "d",
                    "e",
                    "e",
                    "zh",
                    "z",
                    "i",
                    "y",
                    "k",
                    "l",
                    "m",
                    "n",
                    "o",
                    "p",
                    "r",
                    "s",
                    "t",
                    "u",
                    "f",
                    "h",
                    "c",
                    "ch",
                    "sh",
                    "sch",
                    "",
                    "y",
                    "",
                    "e",
                    "yu",
                    "ya",
                ],
                strict=True,
            )
        )
        raw = "".join(
            letters.get(c, c if c.isascii() and c.isalpha() else " ") for c in title.lower()
        )
        return re.sub(r"-+", "-", raw.strip().replace(" ", "-"))

    async def collect(self):
        base = "https://rabota.sber.ru/public/app-candidate-public-api-gateway/api/v1/"
        directory = (await self.fetcher.get(base + "profAreas")).json()
        areas = [area["id"] for area in directory["data"] if classify(area["value"])]
        if not areas:
            raise SourceError("Сбер изменил справочник профессиональных областей.")
        # Site text search tokenizes phrases broadly. Use its role categories,
        # plus alternate spellings to catch postings in other categories.
        for query in (
            {"profAreas": areas},
            {"searchString": "продакт"},
            {"searchString": "product"},
            {"searchString": "project"},
        ):
            offset = 0
            seen = set()
            for _ in range(15):
                r = await self.fetcher.get(
                    "https://rabota.sber.ru/public/app-candidate-public-api-gateway/api/v1/publications",
                    params={"skip": offset, "take": 100, **query},
                )
                envelope = r.json()
                if not envelope.get("success"):
                    raise SourceError("Сбер вернул ошибку поиска.")
                data = envelope["data"]
                items = data["vacancies"]
                ids = {v["internalId"] for v in items}
                if ids and ids <= seen:
                    raise SourceError("Сбер повторяет страницу; сбор неполный.")
                seen.update(ids)
                for item in items:
                    path = f"/search/{self.slug(item['title'])}-{item['internalId']}/"
                    self.add(
                        make(
                            self.name,
                            self.key,
                            item["internalId"],
                            item["title"],
                            safe_url(self.url, path),
                            city=item.get("city", ""),
                            salary=salary_text(
                                {
                                    "from": item.get("salary_min"),
                                    "to": item.get("salary_max"),
                                    "currency": "RUR",
                                    "gross": True,
                                }
                            ),
                            work="Удалённо" if item.get("workScheduleId") == 4 else "",
                            remote=item.get("workScheduleId") == 4,
                            published=item.get("publicationDate"),
                        )
                    )
                offset += len(items)
                if offset >= data["total"]:
                    break
                if not items:
                    raise SourceError("Пустая страница Сбера до окончания выдачи.")
            else:
                self.partial("Достигнут лимит 15 страниц на запрос Сбера.")
