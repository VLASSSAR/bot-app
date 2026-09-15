from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pytest
from lxml import etree

from vacancy_bot.hh import clean, salary_text
from vacancy_bot.matching import classify
from vacancy_bot.models import Collection, Filters, Role, SourceStatus
from vacancy_bot.report import build_report
from vacancy_bot.service import deduplicate
from vacancy_bot.sources.common import company_key, make, safe_url
from vacancy_bot.storage import Storage


@pytest.mark.parametrize(
    "title,role",
    [
        ("Senior Product Manager", Role.PRODUCT),
        ("Product Owner", Role.PRODUCT),
        ("Менеджер по продукту", Role.PRODUCT),
        ("ML-продакт-менеджер", Role.PRODUCT),
        ("Технический продакт-менеджер", Role.TECHNICAL),
        ("Senior Technical Product Manager", Role.TECHNICAL),
        ("Менеджер технических проектов в SAM", Role.PROJECT),
        ("Project Manager", Role.PROJECT),
        ("Руководитель проектов", Role.PROJECT),
        ("Менеджер по проектам", Role.PROJECT),
        ("Продуктовый аналитик", None),
        ("Product Designer", None),
        ("Руководитель продуктовой аналитики", None),
        ("Менеджер технического продукта", Role.TECHNICAL),
        ("Менеджер по продажам продуктов", None),
        ("PM", None),
        ("Technical Project Manager", Role.PROJECT),
    ],
)
def test_classify(title, role):
    assert classify(title) == role


def vacancy(source="Яндекс Карьера", priority=0, vid="1"):
    return make(
        source,
        "yandex",
        vid,
        "Менеджер продукта",
        f"https://yandex.ru/jobs/vacancies/{vid}",
        city="Москва",
        priority=priority,
    )


def test_dedup_keeps_employer_and_links():
    original = vacancy()
    board = replace(vacancy("Getmatch", 1), url="https://getmatch.ru/vacancies/2")
    result = deduplicate([board, original, original])
    assert len(result) == 1
    assert result[0].source == "Яндекс Карьера"
    assert result[0].alternate_urls == (board.url,)


def test_ambiguous_same_title_is_not_merged():
    a, b = vacancy(vid="1"), vacancy(vid="2")
    board = replace(vacancy("Getmatch", 1), url="https://getmatch.ru/vacancies/3")
    assert len(deduplicate([a, b, board])) == 3
    assert len(deduplicate([a, replace(b, city="Казань")])) == 2


def test_persistence_first_seen_and_subscription(tmp_path):
    db = Storage(tmp_path / "db.sqlite")
    now = datetime.now(UTC)
    first = db.observe([vacancy()], now)[0]
    later = db.observe([vacancy()], now + timedelta(days=5))[0]
    assert first.first_seen == later.first_seen == now
    db.subscribe(1, 1, "09:00")
    db.save_filters(1, Filters(days=7, companies=("vk",)))
    restored = Storage(db.path)
    assert restored.filters(1).days == 7
    assert restored.subscriptions()[0]["daily_time"] == "09:00"
    assert restored.filters(2) == Filters()
    restored.unsubscribe(1)
    assert restored.subscriptions() == []


def test_salary_and_untrusted_text():
    assert salary_text(None) == "Не указана"
    assert "на руки" in salary_text({"from": 200000, "to": None, "currency": "RUR", "gross": False})
    assert clean("<b>Python</b> &amp; SQL\x00") == "Python & SQL"
    assert company_key("Агентство для Яндекс") is None
    assert company_key("Т-Банк") == "tbank"
    with pytest.raises(ValueError):
        safe_url("https://getmatch.ru", "//evil.example/job")
    with pytest.raises(ValueError):
        safe_url("https://getmatch.ru", "javascript:alert(1)")


def test_word_report_has_links_dates_and_failure_status():
    now = datetime.now(UTC)
    job = replace(vacancy(), first_seen=now)
    result = Collection(
        [job],
        Filters(),
        now,
        sources=[SourceStatus("Ozon", "https://job.ozon.ru", "error", message="HTTP 403")],
    )
    data = build_report(result, ZoneInfo("Asia/Novosibirsk"))
    with ZipFile(BytesIO(data)) as z:
        xml = etree.fromstring(z.read("word/document.xml"))
        text = "".join(xml.itertext())
        assert "Дата публикации не указана" in text
        assert "Впервые найдена" in text
        assert "Недоступен" in text and "HTTP 403" in text
        rels = z.read("word/_rels/document.xml.rels").decode()
        assert job.url in rels
        assert b"w:hyperlink" in z.read("word/document.xml")


@pytest.mark.parametrize("days", [0, 31, -1])
def test_invalid_period(days):
    with pytest.raises(ValueError):
        Filters(days=days)
