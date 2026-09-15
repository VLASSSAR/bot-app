from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .config import COMPANIES


class Role(StrEnum):
    PRODUCT = "Продуктовый менеджер"
    PROJECT = "Проектный менеджер"
    TECHNICAL = "Технический продакт-менеджер"


@dataclass(frozen=True)
class Filters:
    days: int = 30
    remote_only: bool = False
    companies: tuple[str, ...] = tuple(COMPANIES)
    include_hh: bool = False

    def __post_init__(self):
        if not 1 <= self.days <= 30:
            raise ValueError("Период должен быть от 1 до 30 дней")
        if not self.companies or any(key not in COMPANIES for key in self.companies):
            raise ValueError("Выберите компании из /companies")


@dataclass(frozen=True)
class Vacancy:
    id: str
    title: str
    company: str
    role: Role
    city: str
    work_format: str
    salary: str
    experience: str
    published_at: datetime | None
    url: str
    requirement: str = ""
    responsibility: str = ""
    source: str = "hh.ru"
    priority: int = 2
    first_seen: datetime | None = None
    region_confirmed: bool = False
    remote: bool = False
    alternate_urls: tuple[str, ...] = ()


@dataclass
class SourceStatus:
    name: str
    url: str
    state: str = "ok"
    count: int = 0
    message: str = ""


@dataclass
class Collection:
    vacancies: list[Vacancy]
    filters: Filters
    collected_at: datetime
    warnings: list[str] = field(default_factory=list)
    successful_searches: int = 0
    total_searches: int = 0
    sources: list[SourceStatus] = field(default_factory=list)
