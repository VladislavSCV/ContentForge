"""Одноразовая проверка новых функций (формат Shorts, рубрики, таблица статистики, импорт бота)."""
import asyncio
import os
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import ai, analytics, bot, db, longform, pipeline  # noqa: E402,F401
from app.config import settings  # noqa: E402

# 1. формат Shorts: история с героем против спора
story = "В 1988 году студент написал червя и впервые положил интернет. История случилась из-за одной ошибки."
argument = "Вашему сервису на 100 RPS не нужна Kafka. Очередь добавляет задержку и усложняет эксплуатацию кластера."
assert ai.script_format(story) == "story", ai.script_format(story)
assert ai.script_format(argument) == "argument", ai.script_format(argument)
print("format OK")

# 2. рубрика по столпу
class P:  # минимальный пост для with_rubric
    def __init__(self, text):
        self.source_text = text

assert "AI в разработке" in pipeline.with_rubric("текст", P("How Claude and LLM agents change code review")), "ai"
assert "Инфраструктура" in pipeline.with_rubric("текст", P("Postgres connection pool saturated under load"))
assert pipeline.with_rubric("<i>🏗 Инфраструктура и SRE</i>\n\nтекст", P("Postgres pool")).count("Инфраструктура") == 1
print("rubric OK")


async def main():
    await db.init()
    pid = await db.create(1, "x" * 200, None)
    await db.update(pid, status=db.PUBLISHED, opinion="моя позиция")
    await db.set_tg_stats(pid, 1500, 14, 3)
    tg = await db.tg_stats()
    assert tg[pid]["forwards"] == 14
    await db.set_tg_stats(pid, 1600, 20)       # повторная запись обновляет, а не дублирует
    assert (await db.tg_stats())[pid]["views"] == 1600
    table = analytics.build_post_table(await db.latest(10), [], [], await db.tg_stats())
    assert "пересл. 20" in table and "позиция: да" in table and "(12‰)" in table, table
    print("stats OK")
    print(table.splitlines()[2])

asyncio.run(main())

# 3. habr из материала: план получает материал как [1]
seen = {}

async def fake_generate(ask, schema, system=None, max_tokens=0):
    seen["ask"] = ask
    raise ai.AIError("stop")

ai._generate = fake_generate
try:
    asyncio.run(longform.write_habr_from_material("материал " * 100, "моя позиция"))
except ai.AIError:
    pass
assert "[1] материал" in seen["ask"] and "ПОЗИЦИЯ АВТОРА" in seen["ask"] and "ОДНОГО материала" in seen["ask"]
print("habr_from OK")
print("ALL OK")
