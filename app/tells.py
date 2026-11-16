"""Признаки «текст написал ИИ»: ловятся без модели и без сети.

Площадки режут охват у текстов, которые выглядят шаблонно. Приём «не X, а Y» один раз в тезисе
работает, три раза подряд — штамп. Поэтому считаем повторы, а не запрещаем фразу целиком.
"""

import re

# «не A, а B» / «это не A. Это B» — приём-разоблачение
_NOT_BUT = re.compile(r"\bне\s[^.!?\n]{2,60}?(?:,\s*а\s|[.—–-]\s*(?:это|а)\s)", re.I)
# «без A, без B, без C» — перечисление-тройка
_NO_NO_NO = re.compile(r"\bбез\s+[^,.\n]{2,30},\s*без\s+[^,.\n]{2,30},\s*(?:и\s+)?без\s+", re.I)
_STOCK = re.compile(
    r"в\s+(?:современном\s+)?мире,?\s+где|в\s+эпоху\s|давай(?:те)?\s+разберёмся|важно\s+отметить|стоит\s+отметить|"
    r"не\s+секрет,?\s+что|погрузимся|раскроем|ключев\w+\s+роль|играет\s+важн\w+\s+роль|"
    r"в\s+этой\s+статье|подводя\s+итог|как\s+показывает\s+практика|давай\s+посмотрим", re.I)


_NOT_BUT_EN = re.compile(r"\b(?:is|are|was|were)\s+not\s[^.!?\n]{2,60}?(?:,\s*but\s|[.—–-]\s*(?:it|that|this)\s+(?:is|was)\s)", re.I)
_NO_NO_NO_EN = re.compile(r"\bno\s+[^,.\n]{2,30},\s*no\s+[^,.\n]{2,30},\s*(?:and\s+)?no\s+", re.I)
_STOCK_EN = re.compile(
    r"fast-paced\s+world|let.?s\s+dive|game.?changer|\bunlock(?:ing)?\b|\bdelve|worth\s+noting|in\s+conclusion|"
    r"thrilled\s+to\s+share|in\s+today.?s\s+\w+\s+(?:landscape|world)|ever-evolving|at\s+the\s+end\s+of\s+the\s+day", re.I)


def ai_tells_en(text: str) -> list[str]:
    """Английский вариант ai_tells для постов LinkedIn на английском."""
    out = []
    nb = len(_NOT_BUT_EN.findall(text))
    if nb >= 3:
        out.append(f"'is not X, it is Y' {nb} times: keep one, in the thesis")
    if _NO_NO_NO_EN.search(text):
        out.append("'no A, no B, no C' triplet: an AI tell, say one thing")
    stock = sorted({m.group(0).lower() for m in _STOCK_EN.finditer(text)})
    if stock:
        out.append("stock phrases: " + ", ".join(f"«{s}»" for s in stock[:4]))
    return out


def ai_tells(text: str) -> list[str]:
    """Замечания к тексту: пустой список — штампов не нашли."""
    out = []
    nb = len(_NOT_BUT.findall(text))
    if nb >= 3:
        out.append(f"приём «не X, а Y» {nb} раза: оставь один, в тезисе")
    if _NO_NO_NO.search(text):
        out.append("перечисление «без A, без B, без C»: штамп ИИ, скажи про одно")
    stock = sorted({m.group(0).lower() for m in _STOCK.finditer(text)})
    if stock:
        out.append("штампы: " + ", ".join(f"«{s}»" for s in stock[:4]))
    return out
