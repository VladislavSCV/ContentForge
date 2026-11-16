"""Поведение веток профиля без сети: scene_plan, script_format, with_rubric. Печатает JSON.
Сравнение «до/после» рефакторинга: запустить на старом коде и на новом, результаты должны совпасть.
  CONTENT_PROFILE=general python tests/behavior_snapshot.py
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path.cwd()))   # код берётся из текущей папки: так можно прогнать старый срез из git archive

from datetime import datetime, timezone  # noqa: E402

from app import ai, learning, pipeline, schemas, topics  # noqa: E402

SRC = ("В 2019 году команда из 12 человек потеряла 47 минут. Исследование 2021 года показало, что стресс связан с "
       "памятью. Был герой, был поворот, и неожиданно всё изменилось. Kubernetes 5 раз упал.")
PKG = SimpleNamespace(youtube_shorts_stream=SimpleNamespace(
    full_audio_script="В 2019 году потеряли 47 минут. Пять раз упал кластер.", youtube_title="Как упал кластер #shorts"))


def plan_variants():
    yield schemas.ScenePlan(layout="claim", myth="Больше 12 серверов быстрее", truth="Быстрее становится 47 минут",
                            flow=["Запрос", "Очередь", "Сервис", "База"], flow_icons=["box", "queue", "xx", "db"],
                            hook_big="47 минут", hook_small="простоя", problem_before="Падал", problem_after="Терял",
                            steps=["Найди узкое место", "Подпишись на телеграм"], code="```yaml\nkind: Pod\n```",
                            code_language="YAML", stock_queries=[])
    yield schemas.ScenePlan(hook_big="Без цифр", hook_small="", problem_before="", problem_after="", steps=[],
                            code="нет", stock_queries=["office", "серверная"])


async def main():
    print('# app from', ai.__file__, file=sys.stderr)
    out = {"script_format": [ai.script_format(SRC), ai.script_format("Обычный текст без сюжета.")], "scene": []}
    for plan in plan_variants():
        async def fake(*a, _p=plan, **k):
            return _p.model_copy(deep=True)
        ai._generate = fake
        out["scene"].append((await ai.scene_plan(SRC, PKG)).model_dump())
    post = SimpleNamespace(source_text="Kubernetes упал, найм сократили, layoffs в 2024")
    out["rubric"] = pipeline.with_rubric("<b>Заголовок</b>\nТекст", post)
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    titles = ["Postmortem: outage and rollback at scale", "New study: sleep and memory in the brain", "Layoffs hit tech job market",
              "How vLLM inference cut GPU costs", "Why stress and burnout hit the economy", "Kubernetes helm and gitops migration",
              "Our Copilot code review experiment", "Quantum paradox in ancient DNA", "Webinar: register now", "Cooking pasta"]
    cands = [topics.Candidate(t, f"https://x/{i}", "Points: 320 # Comments: 120", "src", now) for i, t in enumerate(titles)]
    for c in cands:
        c.pillar = topics.pillar_of(c)
    out["topics"] = {"pillars": [c.pillar for c in cands], "scores": [round(topics.score(c, now), 4) for c in cands],
                     "adj_scores": [round(topics.score(c, now, {"t0": 1.0, "t2": -0.5, "t4": 0.7}), 4) for c in cands],
                     "pick": [c.title for c in topics.pick(cands, set(), 3, now)],
                     "pick_flat": [c.title for c in topics.pick(cands, set(), 3, now, by_pillar=False)],
                     "sources": len(topics.load_sources())}
    try:
        from app import bot as _bot
        out["help"] = _bot.HELP
    except ImportError as e:   # старый срез без нужных модулей
        out["help"] = f"import error {e}"
    out["learning_groups"] = sorted(learning._groups())
    print(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True))


asyncio.run(main())
