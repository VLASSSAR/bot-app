import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html

from ..config import COMPANIES, COMPANY_ALIASES
from ..hh import SourceError, clean
from ..matching import classify
from ..models import SourceStatus, Vacancy


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, delay: float = 0.35):
        self.client = client
        self.delay = delay

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def request(self, method, url, **kwargs):
        for attempt in range(3):
            await asyncio.sleep(self.delay)
            try:
                r = await self.client.request(method, url, **kwargs)
                if r.status_code in (401, 403):
                    raise SourceError(f"Источник ограничил доступ (HTTP {r.status_code}).")
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt < 2:
                        try:
                            delay = float(r.headers.get("Retry-After", 2**attempt))
                        except ValueError:
                            delay = 2**attempt
                        await asyncio.sleep(min(30, max(0, delay)))
                        continue
                r.raise_for_status()
                return r
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise SourceError("Источник не ответил после трёх попыток.") from exc
                await asyncio.sleep(2**attempt)
        raise SourceError("Лимит запросов источника или временный сбой.")


def company_key(name):
    def norm(x):
        return re.sub(r"[\W_]+", "", x.casefold())

    for key, aliases in COMPANY_ALIASES.items():
        if norm(name) in {norm(a) for a in aliases}:
            return key
    return None


def date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Getmatch timestamps have no timezone. Preserve the calendar date as UTC,
        # not an invented exact local publication time; reports display dates only.
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def safe_url(base, path):
    url = urljoin(base, path)
    p, b = urlparse(url), urlparse(base)
    if p.scheme != "https" or p.hostname != b.hostname or p.username or p.password:
        raise ValueError("Unsafe or cross-domain vacancy URL")
    return url.split("#")[0].split("?")[0]


# Explicit foreign locations are excluded. Unknown cities stay visibly unconfirmed.
FOREIGN = re.compile(
    r"алмат|астан|казахстан|ташкент|узбекистан|минск|беларус|ереван|армени|"
    r"тбилиси|грузи|белград|серби|дубай|кипр|лимассол|лима[сс]ол|"
    r"казанстан|bishkek|london|berlin|cyprus|yerevan|tashkent",
    re.I,
)
RUSSIAN = re.compile(
    r"росси|russia|москва|moscow|петербург|новосибирск|казань|казани|"
    r"екатеринбург|нижний новгород|самара|ростов|краснодар|сочи|воронеж|"
    r"уфа|томск|пермь|тюмень|омск|челябинск|красноярск|зеленоград|"
    r"саратов|волгоград|калининград|ярославль|тула|ижевск|владивосток",
    re.I,
)


def make(
    source,
    key,
    vid,
    title,
    url,
    city="",
    work="",
    salary="",
    experience="",
    published=None,
    summary="",
    remote=False,
    region=None,
    priority=0,
):
    role = classify(title)
    if not role:
        return None
    city = clean(city)
    if (FOREIGN.search(city) and not RUSSIAN.search(city)) or (not city and FOREIGN.search(title)):
        return None
    return Vacancy(
        str(vid),
        clean(title),
        COMPANIES[key][0],
        role,
        city or "Не указан",
        clean(work) or "Не указан",
        clean(salary) or "Не указана",
        clean(experience) or "Не указан",
        date(published),
        url,
        responsibility=clean(summary)[:500],
        source=source,
        priority=priority,
        remote=remote,
        region_confirmed=bool(RUSSIAN.search(city)) if region is None else region,
    )


def document(text):
    return html.fromstring(text)


def next_data(text, element_id="__NEXT_DATA__"):
    nodes = document(text).xpath(f'//script[@id="{element_id}"]/text()')
    if not nodes:
        raise SourceError("Структура страницы изменилась или сайт требует проверку браузера.")
    return json.loads(nodes[0])


def rsc_text(text):
    parts = []
    for el in document(text).xpath("//script"):
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\);?", el.text or "", re.S)
        if match:
            part = json.loads(match[1])
            if len(part) > 1 and isinstance(part[1], str):
                parts.append(part[1])
    return "".join(parts)


def json_field(text, key):
    # Decode data from the page's serialized state without executing JavaScript.
    for match in re.finditer('"' + re.escape(key) + r'"\s*:', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end() :].lstrip())
            yield value
        except ValueError:
            continue


class Adapter:
    name = ""
    url = ""
    key = None

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.items = []
        self.status = SourceStatus(self.name, self.url)

    def add(self, vacancy):
        if vacancy is not None and vacancy.url not in {v.url for v in self.items}:
            self.items.append(vacancy)

    def partial(self, reason):
        self.status.state = "partial"
        self.status.message = reason

    async def run(self):
        try:
            await self.collect()
        except (SourceError, ValueError, KeyError, TypeError, IndexError) as exc:
            self.status.state = "partial" if self.items else "error"
            self.status.message = (
                str(exc)
                if isinstance(exc, SourceError)
                else "Изменилась структура данных источника."
            )
        self.status.count = len(self.items)
        return self.items, self.status
