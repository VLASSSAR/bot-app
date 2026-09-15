import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Explicit IDs avoid matching agencies or similarly named organizations.
# Additional subsidiaries can be added to the tuple after checking their HH profile.
COMPANIES = {
    "yandex": ("Яндекс", ("1740",)),
    "vk": ("VK", ("15478",)),
    "sber": ("Сбер", ("3529",)),
    "tbank": ("Т-Банк", ("78638",)),
    "ozon": ("Ozon", ("2180",)),
    "wildberries": ("Wildberries", ("87021",)),
    "avito": ("Авито", ("84585",)),
    "mts": ("МТС", ("3776",)),
}


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    user_agent: str
    database_path: Path
    timezone: ZoneInfo
    hh_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, require_telegram: bool = True):
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if require_telegram and not token:
            raise ValueError("Укажите TELEGRAM_BOT_TOKEN в .env")
        ids = frozenset(
            int(v.strip()) for v in os.getenv("ALLOWED_USER_IDS", "").split(",") if v.strip()
        )
        if any(v <= 0 for v in ids):
            raise ValueError("ALLOWED_USER_IDS должен содержать положительные ID пользователей")
        return cls(
            token,
            ids,
            os.getenv("HTTP_USER_AGENT", "bot-app/0.1 (+https://github.com/VLASSSAR/bot-app)"),
            Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
            ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Novosibirsk")),
            os.getenv("HH_ACCESS_TOKEN", "").strip(),
        )


# Exact aliases, not substring matching: agencies and anonymous employers are excluded.
COMPANY_ALIASES = {
    "yandex": ("Яндекс", "Yandex", "Яндекс Маркет", "Яндекс Cloud", "Yandex Cloud"),
    "vk": ("VK", "ВКонтакте", "VK Tech", "Mail.ru Group"),
    "sber": ("Сбер", "СБЕР", "Сбербанк", "Sber", "СберЗдоровье", "СберТех", "СберТехнологии"),
    "tbank": ("Т-Банк", "Т Банк", "Тинькофф", "T-Банк", "Tinkoff", "Т-Технологии"),
    "ozon": ("Ozon", "Ozon Tech", "Ozon Банк", "Ozon Fintech"),
    "wildberries": ("Wildberries", "RWB", "RWB (Wildberries & Russ)", "Wildberries & Russ"),
    "avito": ("Авито", "Avito"),
    "mts": ("МТС", "MTS", "МТС Банк", "МТС-Банк", "МТС Диджитал", "MTS Digital"),
}
