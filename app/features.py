"""Площадки и дополнительные возможности, которые можно сузить для конкретной команды: FEATURES в .env.

    FEATURES=*                      по умолчанию: всё включено
    FEATURES=habr,reddit            включено только перечисленное
Telegram и YouTube здесь не перечислены: это ядро, оно управляется токенами. Новую площадку добавляем,
дописав имя в KNOWN и поставив проверку `features.enabled("имя")` в месте, где она появляется в боте.
"""

KNOWN = {
    "linkedin": "черновик поста для LinkedIn, кнопка в карточке, импорт выгрузки .xlsx",
    "habr": "статья для Хабра (/habr, /habr_from, ежемесячная сборка)",
    "devto": "английская версия статьи для dev.to (/devto)",
    "reddit": "подборка обсуждений Reddit и заготовка ответа (/reddit)",
    "week": "итоги недели и таблица охватов (/week, /stats, /tgstats)",
    "talk": "напоминание про внутренний доклад",
    "longvideo": "ролик на 5–7 минут (/longvideo)",
}


def parse(raw: str) -> set[str]:
    """Строка из .env → набор имён. Пусто, «*» и «all» — всё. Неизвестное имя — ошибка с перечнем."""
    items = {x.strip().lower() for x in raw.replace(";", ",").split(",") if x.strip()}
    if not items or items & {"*", "all"}:
        return set(KNOWN)
    unknown = items - set(KNOWN)
    if unknown:
        raise ValueError(f"FEATURES: неизвестные {sorted(unknown)}; есть {sorted(KNOWN)} или *")
    return items


def enabled(name: str) -> bool:
    from .config import settings   # поздний импорт: config сам проверяет FEATURES через parse()
    if name not in KNOWN:
        raise KeyError(f"неизвестная функция {name!r}")
    return name in parse(settings.features)
