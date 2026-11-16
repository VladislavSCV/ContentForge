"""Призыв в конце Shorts — шаблон, а не импровизация модели.

Ссылки в описании и комментариях Shorts YouTube сделал некликабельными ещё в 2023-м
(защита от спама), поэтому просить «перейди по ссылке» бессмысленно: URL придётся
перепечатывать вручную. Работают два действия: подписка в один тап и поиск канала
по @handle внутри Telegram. Название канала должно звучать одинаково в каждом ролике,
поэтому призыв задаётся здесь, а не пишется заново при каждой генерации.
"""

from .config import settings

SUBSCRIBE, TELEGRAM = "subscribe", "telegram"
MODES = (SUBSCRIBE, TELEGRAM)

LABELS = {SUBSCRIBE: "подписка на YouTube", TELEGRAM: "переход в Telegram"}


def normalize(mode: str | None) -> str:
    return mode if mode in MODES else settings.cta_mode if settings.cta_mode in MODES else SUBSCRIBE


def voice_line(mode: str | None = None) -> str:
    """Последняя фраза диктора.

    Название канала вслух не произносим: «name2go8» русский синтезатор читает по буквам
    или с чужим акцентом. Вместо него отсылаем к профилю канала — единственное место,
    где ссылка кликабельна, в отличие от описаний и комментариев Shorts.
    """
    if normalize(mode) == TELEGRAM:
        return "Готовые конфиги забирай в телеграме. Ссылка в профиле канала."
    return "Подпишись, чтобы не пропустить следующий разбор. А конфиги и исходники ждут в телеграме, ссылка в профиле канала."


def screen(mode: str | None = None) -> dict:
    """Что показываем в финальной сцене ролика."""
    if normalize(mode) == TELEGRAM:
        return {
            "title": settings.brand_name,
            "sub": "исходники и конфиги — в Telegram",
            "pill": settings.brand_handle,
        }
    return {
        "title": "Подпишись",
        "sub": "а конфиги и исходники — в Telegram",
        "pill": settings.brand_handle,
    }
