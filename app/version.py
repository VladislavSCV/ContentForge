"""Версия работающего бота: что выложено, когда и куда, и сколько он уже работает.

Файл app/_version.txt (формат «дата|отпечаток|сервер») пишет scripts/deploy_remote.sh при выкладке и кладёт
его в образ вместе с кодом. Отпечаток — короткий хэш содержимого кода, подсказок, словаря и ресурсов, поэтому
он меняется, только когда реально поменялось что-то в боте. Нет файла — значит, бот запущен не выкладкой
(например, локально).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings

FILE = Path(__file__).with_name("_version.txt")
STARTED = datetime.now(timezone.utc)


@dataclass
class Info:
    built: str = ""
    digest: str = ""
    target: str = ""

    @property
    def known(self) -> bool:
        return bool(self.digest)


def parse(text: str) -> Info:
    parts = [p.strip() for p in text.strip().split("|")]
    return Info(*parts[:3]) if len(parts) >= 2 and parts[1] else Info()


def read() -> Info:
    try:
        return parse(FILE.read_text(encoding="utf-8"))
    except OSError:
        return Info()


def uptime(now: datetime | None = None) -> str:
    secs = int(((now or datetime.now(timezone.utc)) - STARTED).total_seconds())
    d, rest = divmod(secs, 86400)
    h, m = divmod(rest // 60, 60)
    return " ".join(x for x in (f"{d} д" if d else "", f"{h} ч" if h or d else "", f"{m} мин") if x)


def describe(now: datetime | None = None) -> str:
    info = read()
    local = (STARTED + timedelta(hours=settings.schedule_utc_offset)).strftime("%d.%m %H:%M")
    if info.known:
        head = [f"🧩 Версия: {info.digest}", f"Выложена: {info.built}" + (f" на {info.target}" if info.target else "")]
    else:
        head = ["🧩 Версия не указана: бот запущен не через scripts/deploy_remote.sh (скорее всего, локально)"]
    return "\n".join([*head, f"Запущен: {local} (местное), работает {uptime(now)}"])


def short() -> str:
    """Одной строкой для сообщений о запуске."""
    info = read()
    return f"версия {info.digest}" if info.known else "версия не указана"
