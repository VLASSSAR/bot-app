from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from vacancy_bot.bot import BotHandlers, build_application
from vacancy_bot.config import Settings
from vacancy_bot.storage import Storage


def settings(tmp_path):
    return Settings(
        "123456789:TESTTOKEN",
        frozenset({123}),
        "tests/1",
        tmp_path / "bot.sqlite",
        ZoneInfo("Asia/Novosibirsk"),
    )


def update(uid=123, text="/period 7", chat_type="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=uid, type=chat_type),
        effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


async def test_unauthorized_cannot_collect(tmp_path):
    service = SimpleNamespace(collect=AsyncMock())
    h = BotHandlers(settings(tmp_path), Storage(tmp_path / "db"), service)
    u = update(uid=99)
    await h.report(u, SimpleNamespace(bot=AsyncMock()))
    service.collect.assert_not_called()
    assert "Доступ закрыт" in u.effective_message.reply_text.call_args.args[0]


async def test_period_persists_and_invalid_value_does_not_change(tmp_path):
    db = Storage(tmp_path / "db")
    h = BotHandlers(settings(tmp_path), db, None)
    await h.change(update(), SimpleNamespace(args=["7"]))
    assert db.filters(123).days == 7
    await h.change(update(), SimpleNamespace(args=["0"]))
    assert db.filters(123).days == 7


def test_schedule_preserves_timezone_and_replaces_duplicate(tmp_path):
    app = build_application(settings(tmp_path))
    h = BotHandlers(settings(tmp_path), Storage(tmp_path / "db"), None)
    h.schedule(app, 123, 123, "09:00")
    job = app.job_queue.get_jobs_by_name("daily:123")[0]
    assert str(job.job.trigger.timezone) == "Asia/Novosibirsk"
    h.schedule(app, 123, 123, "10:00")
    assert len(app.job_queue.get_jobs_by_name("daily:123")) == 1
