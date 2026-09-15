import asyncio
import logging
from dataclasses import replace
from datetime import time
from io import BytesIO

from telegram import BotCommand, ReplyKeyboardMarkup
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import COMPANIES
from .hh import SourceError
from .report import build_report
from .service import Service
from .storage import Storage

log = logging.getLogger(__name__)
HELP = """Собираю вакансии в российском бигтехе и отправляю Word-отчет.

/report — сформировать отчет
/period 7 — период от 1 до 30 дней
/remote on или off — только удалённо или все форматы
/companies — список компаний и инструкция выбора
/hh on или off — дополнительный источник hh.ru (по умолчанию выключен)
/subscribe 09:00 — ежедневный отчет в вашем часовом поясе бота
/unsubscribe — отключить рассылку
/settings — текущие настройки
/whoami — ваш Telegram ID
/help — справка

Источники: карьерные сайты → Getmatch и Хабр Карьера → hh.ru.
Вакансии без даты публикации показаны отдельно по дате первого обнаружения."""


class BotHandlers:
    def __init__(self, settings, storage, service):
        self.settings, self.storage, self.service = settings, storage, service
        self.busy = set()

    async def allowed(self, update):
        if not update.effective_message or not update.effective_user or not update.effective_chat:
            return False
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text("Используйте бота в личном чате.")
            return False
        if update.effective_user.id not in self.settings.allowed_user_ids:
            await update.effective_message.reply_text(
                "Доступ закрыт. Узнайте свой ID через /whoami "
                "и добавьте его в ALLOWED_USER_IDS на сервере."
            )
            return False
        return True

    async def whoami(self, update, context):
        if (
            update.effective_user
            and update.effective_chat
            and update.effective_chat.type == "private"
        ):
            await update.effective_message.reply_text(
                f"Ваш Telegram ID: {update.effective_user.id}"
            )

    async def help(self, update, context):
        if await self.allowed(update):
            await update.effective_message.reply_text(
                HELP,
                reply_markup=ReplyKeyboardMarkup(
                    [["Получить отчет", "Настройки"]], resize_keyboard=True
                ),
            )

    async def settings_command(self, update, context):
        if not await self.allowed(update):
            return
        f = self.storage.filters(update.effective_user.id)
        sub = next(
            (
                s["daily_time"]
                for s in self.storage.subscriptions()
                if s["user_id"] == update.effective_user.id
            ),
            "выключена",
        )
        await update.effective_message.reply_text(
            f"Период: {f.days} дней\nТолько удалённо: {'да' if f.remote_only else 'нет'}\n"
            f"Компании: {', '.join(COMPANIES[k][0] for k in f.companies)}\n"
            f"hh.ru: {'включен' if f.include_hh else 'выключен'}\n"
            f"Рассылка: {sub}\nЧасовой пояс: {self.settings.timezone.key}"
        )

    async def change(self, update, context):
        if not await self.allowed(update):
            return
        uid = update.effective_user.id
        f = self.storage.filters(uid)
        command = update.effective_message.text.split()[0].split("@")[0][1:]
        try:
            if command == "companies" and not context.args:
                await update.effective_message.reply_text(
                    "\n".join(f"{k} — {v[0]}" for k, v in COMPANIES.items())
                    + "\n\nВыбор: /companies yandex vk sber\nВсе: /companies all"
                )
                return
            if command == "period" and len(context.args) == 1:
                f = replace(f, days=int(context.args[0]))
            elif command in ("remote", "hh") and context.args in (["on"], ["off"]):
                field = "remote_only" if command == "remote" else "include_hh"
                f = replace(f, **{field: context.args == ["on"]})
            elif command == "companies" and context.args:
                f = replace(
                    f,
                    companies=tuple(COMPANIES)
                    if context.args == ["all"]
                    else tuple(dict.fromkeys(context.args)),
                )
            else:
                raise ValueError("Проверьте формат команды в /help")
            self.storage.save_filters(uid, f)
            await self.settings_command(update, context)
        except ValueError as exc:
            await update.effective_message.reply_text(
                str(exc)
                if "invalid literal" not in str(exc)
                else "Период — целое число от 1 до 30."
            )

    async def send_report(self, bot, uid, chat_id):
        if uid in self.busy:
            await bot.send_message(chat_id, "Предыдущий отчет ещё готовится.")
            return
        self.busy.add(uid)
        try:
            result = await self.service.collect(self.storage.filters(uid))
            content = await asyncio.to_thread(build_report, result, self.settings.timezone)
            filename = f"vacancies_{result.collected_at:%Y-%m-%d}.docx"
            failed = sum(s.state != "ok" for s in result.sources)
            await bot.send_document(
                chat_id,
                document=BytesIO(content),
                filename=filename,
                caption=f"Найдено: {len(result.vacancies)}. Источников с ограничениями: {failed}. "
                "Подробности и ссылки — в отчете.",
            )
        except SourceError as exc:
            await bot.send_message(chat_id, str(exc))
        except Forbidden:
            self.storage.unsubscribe(uid)
        except Exception as exc:
            # Log only exception class: HTTP/Telegram exceptions can contain token-bearing URLs.
            log.error("Report failed: %s", type(exc).__name__)
            try:
                await bot.send_message(
                    chat_id, "Не удалось подготовить или отправить отчет. Попробуйте позже."
                )
            except TelegramError:
                pass
        finally:
            self.busy.discard(uid)

    async def report(self, update, context):
        if not await self.allowed(update):
            return
        await update.effective_message.reply_text(
            "Собираю вакансии. Проверка сайтов может занять несколько минут."
        )
        await self.send_report(context.bot, update.effective_user.id, update.effective_chat.id)

    def schedule(self, app, uid, chat_id, hhmm):
        for job in app.job_queue.get_jobs_by_name(f"daily:{uid}"):
            job.schedule_removal()
        hour, minute = map(int, hhmm.split(":"))
        app.job_queue.run_daily(
            self.daily,
            time(hour, minute, tzinfo=self.settings.timezone),
            chat_id=chat_id,
            user_id=uid,
            name=f"daily:{uid}",
            job_kwargs={"misfire_grace_time": 3600, "max_instances": 1},
        )

    async def subscribe(self, update, context):
        if not await self.allowed(update):
            return
        try:
            if len(context.args) != 1:
                raise ValueError
            value = context.args[0]
            hour, minute = map(int, value.split(":"))
            time(hour, minute)
        except ValueError:
            await update.effective_message.reply_text("Формат: /subscribe 09:00")
            return
        hhmm = f"{hour:02}:{minute:02}"
        uid, cid = update.effective_user.id, update.effective_chat.id
        self.storage.subscribe(uid, cid, hhmm)
        self.schedule(context.application, uid, cid, hhmm)
        await update.effective_message.reply_text(
            f"Отчет каждый день в {hhmm} ({self.settings.timezone.key})."
        )

    async def unsubscribe(self, update, context):
        if not await self.allowed(update):
            return
        uid = update.effective_user.id
        self.storage.unsubscribe(uid)
        for job in context.job_queue.get_jobs_by_name(f"daily:{uid}"):
            job.schedule_removal()
        await update.effective_message.reply_text("Ежедневные отчеты отключены.")

    async def daily(self, context: ContextTypes.DEFAULT_TYPE):
        job = context.job
        if job.user_id in self.settings.allowed_user_ids:
            await self.send_report(context.bot, job.user_id, job.chat_id)

    async def post_init(self, app):
        await app.bot.set_my_commands(
            [
                BotCommand("report", "Word-отчет"),
                BotCommand("settings", "Настройки"),
                BotCommand("subscribe", "Ежедневная подписка"),
                BotCommand("unsubscribe", "Отключить подписку"),
                BotCommand("help", "Справка"),
                BotCommand("whoami", "Узнать свой ID"),
            ]
        )
        for row in self.storage.subscriptions():
            if row["user_id"] in self.settings.allowed_user_ids:
                self.schedule(app, row["user_id"], row["chat_id"], row["daily_time"])

    async def text(self, update, context):
        if update.effective_message.text == "Получить отчет":
            await self.report(update, context)
        elif update.effective_message.text == "Настройки":
            await self.settings_command(update, context)
        else:
            await self.help(update, context)

    async def error(self, update, context):
        log.error("Telegram handler failed: %s", type(context.error).__name__)


def build_application(settings):
    storage = Storage(settings.database_path)
    handlers = BotHandlers(settings, storage, Service(settings, storage))
    app = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(8)
        .post_init(handlers.post_init)
        .build()
    )
    for names, callback in [
        (["start", "help"], handlers.help),
        ("whoami", handlers.whoami),
        ("report", handlers.report),
        ("settings", handlers.settings_command),
        (["period", "remote", "companies", "hh"], handlers.change),
        ("subscribe", handlers.subscribe),
        ("unsubscribe", handlers.unsubscribe),
    ]:
        app.add_handler(CommandHandler(names, callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text))
    app.add_error_handler(handlers.error)
    return app
