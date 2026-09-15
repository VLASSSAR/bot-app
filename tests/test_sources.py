import json
from unittest.mock import AsyncMock

import httpx
import pytest

from vacancy_bot.hh import HHClient, SourceError
from vacancy_bot.sources.boards import Getmatch, Habr
from vacancy_bot.sources.common import Fetcher, rsc_text
from vacancy_bot.sources.employers import MTS, VK, Sber, TBank, Yandex


def fetcher(handler):
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), delay=0)


async def test_vk_paginates_and_keeps_no_date():
    offsets = []

    def handle(req):
        offset = int(req.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200,
            json={
                "count": 2,
                "results": [
                    {
                        "id": offset + 1,
                        "title": "Технический продакт-менеджер",
                        "town": {"name": "Москва"},
                        "remote": True,
                        "work_format": "Удалённо",
                    }
                ],
            },
        )

    adapter = VK(fetcher(handle))
    jobs, status = await adapter.run()
    assert offsets == [0, 1] and len(jobs) == 2 and status.state == "ok"
    assert all(v.published_at is None for v in jobs)
    await adapter.fetcher.client.aclose()


async def test_getmatch_injected_offers_do_not_skip_offset():
    offsets = []

    def handle(req):
        offset = int(req.url.params["offset"])
        offsets.append(offset)

        def item(i):
            return {
                "id": i,
                "position": "Product Manager",
                "is_active": True,
                "url": f"/vacancies/{i}",
                "company": {"name": "Яндекс"},
                "published_at": "2026-09-14T10:00:00",
                "salary_hidden": True,
                "salary_display_from": 999999,
                "location_requirements": [
                    {
                        "format": "remote",
                        "city": "Москва",
                        "country": "Россия",
                        "ancestors": ["russia"],
                    }
                ],
            }

        return httpx.Response(
            200,
            json={
                "meta": {"limit": 2, "total": 4},
                "offers": [item(offset + 1), item(offset + 2), item(99)],
            },
        )

    adapter = Getmatch(fetcher(handle))
    jobs, status = await adapter.run()
    assert offsets == [0, 2] and len(jobs) == 5 and status.state == "ok"
    assert all(v.salary == "Не указана" for v in jobs)
    await adapter.fetcher.client.aclose()


async def test_source_error_is_not_successful_empty_search():
    a = VK(fetcher(lambda _: httpx.Response(403)))
    jobs, status = await a.run()
    assert jobs == [] and status.state == "error" and "403" in status.message
    await a.fetcher.client.aclose()


async def test_retry_rate_limit_then_success(monkeypatch):
    monkeypatch.setattr("vacancy_bot.sources.common.asyncio.sleep", AsyncMock())
    calls = []

    def handle(req):
        calls.append(req)
        return (
            httpx.Response(429, headers={"Retry-After": "1"})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    f = fetcher(handle)
    assert (await f.get("https://example.com")).status_code == 200 and len(calls) == 2
    await f.client.aclose()


async def test_yandex_cursor_and_modified_is_not_publication_date():
    calls = []

    def handle(req):
        calls.append(req.url)
        first = "cursor" not in req.url.params
        data = {
            "results": [
                {
                    "id": 1 if first else 2,
                    "title": "Менеджер продукта",
                    "publication_slug_url": "product-" + ("1" if first else "2"),
                    "modified": "2026-09-01",
                    "vacancy": {"cities": [{"name": "Москва"}]},
                }
            ],
            "next": "http://internal.example/api?cursor=second" if first else None,
        }
        text = "<script>self.__next_f.push(" + json.dumps([1, json.dumps(data)]) + ")</script>"
        return httpx.Response(200, text=text)

    a = Yandex(fetcher(handle))
    jobs, status = await a.run()
    assert len(calls) == 6 and all(u.host == "yandex.ru" for u in calls)
    assert len(jobs) == 2 and status.state == "ok"
    assert all(v.published_at is None for v in jobs)
    await a.fetcher.client.aclose()


async def test_habr_null_locations_and_predicted_salary():
    obj = {
        "vacancies": {
            "list": [
                {
                    "id": 1,
                    "title": "Product Manager",
                    "company": {"title": "VK"},
                    "href": "/vacancies/1",
                    "locations": None,
                    "salary": None,
                    "predictedSalary": {"formatted": "500 000 ₽"},
                    "qualification": None,
                }
            ],
            "pagination": {},
        }
    }
    a = Habr(
        fetcher(
            lambda _: httpx.Response(
                200, text='<script type="application/json">' + json.dumps(obj) + "</script>"
            )
        )
    )
    jobs, status = await a.run()
    assert status.state == "ok" and len(jobs) == 1 and jobs[0].salary == "Не указана"
    await a.fetcher.client.aclose()


async def test_mts_uses_string_slug_not_rounded_numeric_id():
    a = MTS(
        fetcher(
            lambda _: httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 123000000000000000,
                            "slug": "123000000000000007",
                            "title": "Менеджер проекта",
                            "isActive": True,
                        }
                    ],
                    "meta": {"pagination": {"pageCount": 1}},
                },
            )
        )
    )
    jobs, status = await a.run()
    assert status.state == "ok" and jobs[0].url.endswith("123000000000000007")
    await a.fetcher.client.aclose()


async def test_tbank_pages_and_read_only_post():
    calls = []

    def handle(req):
        body = json.loads(req.content)
        calls.append(body)
        offset = body["pagination"]["offset"]
        return httpx.Response(
            200,
            json={
                "payload": {
                    "vacancies": [
                        {
                            "urlSlug": str(offset),
                            "seoSlug": "product",
                            "title": "Продакт-менеджер",
                            "category": "tcareer_it",
                            "subtitle": "Москва",
                        }
                    ],
                    "nextPagination": {"offset": offset + 1, "isFinished": offset == 1},
                }
            },
        )

    a = TBank(fetcher(handle))
    jobs, status = await a.run()
    assert len(calls) == 2 and len(jobs) == 2 and status.state == "ok"
    await a.fetcher.client.aclose()


def test_sber_slug_and_rsc_does_not_execute_js():
    assert Sber.slug("Менеджер продукта") == "menedzher-produkta"
    assert rsc_text('<script>alert("test")</script>') == ""


async def test_hh_access_denial_has_safe_message():
    async with httpx.AsyncClient(
        base_url="https://api.hh.ru", transport=httpx.MockTransport(lambda _: httpx.Response(403))
    ) as client:
        with pytest.raises(SourceError, match="401/403"):
            await HHClient(client, pause=0).request({})


async def test_sber_uses_role_directory_and_pagination():
    calls = []

    def handle(req):
        if req.url.path.endswith("/profAreas"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "product-role", "value": "ИТ:Менеджер продукта"},
                        {"id": "sales", "value": "Менеджер по продажам"},
                    ]
                },
            )
        calls.append(req.url)
        if "profAreas" not in req.url.params:
            return httpx.Response(
                200, json={"success": True, "data": {"total": 0, "vacancies": []}}
            )
        offset = int(req.url.params["skip"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "total": 2,
                    "vacancies": [
                        {
                            "internalId": offset + 1,
                            "title": "Менеджер продукта",
                            "city": "Москва",
                            "workScheduleId": 4,
                            "publicationDate": "2026-09-01T12:00:00Z",
                        }
                    ],
                },
            },
        )

    a = Sber(fetcher(handle))
    jobs, status = await a.run()
    assert status.state == "ok" and len(jobs) == 2
    assert calls[0].params.get_list("profAreas") == ["product-role"]
    assert calls[1].params["skip"] == "1"
    assert jobs[0].remote
    await a.fetcher.client.aclose()
