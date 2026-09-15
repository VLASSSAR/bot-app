import argparse
import asyncio
import logging
from pathlib import Path

from .config import Settings
from .models import Filters
from .report import build_report
from .service import Service
from .storage import Storage


async def collect_file(settings, output):
    service = Service(settings, Storage(settings.database_path))
    result = await service.collect(Filters(), force=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_report(result, settings.timezone))
    print(f"Отчет сохранён: {output}. Вакансий: {len(result.vacancies)}")
    for status in result.sources:
        print(f"{status.name}: {status.state}. {status.message}")


def main():
    parser = argparse.ArgumentParser(description="Вакансии российского бигтеха в Telegram и Word")
    parser.add_argument("--report", type=Path, help="Собрать Word-отчет без Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        settings = Settings.from_env(require_telegram=args.report is None)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    if args.report:
        asyncio.run(collect_file(settings, args.report))
    else:
        from .bot import build_application

        build_application(settings).run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
