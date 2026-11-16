"""Снять несколько кадров композиции в PNG — быстрый просмотр дизайна без полного рендера.

    python scripts/preview_frames.py            # кадры по одному из каждой сцены
    python scripts/preview_frames.py 2 9 20 34  # свои секунды
Результат: data/media/preview/*.png и склейка sheet.png (нужен ffmpeg).
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS"):
    os.environ.setdefault(k, "1")

from app import video  # noqa: E402
from app.config import settings  # noqa: E402
from app.schemas import ScenePlan  # noqa: E402
from app.tts import Word  # noqa: E402

SCRIPT = (
    "Представь, что ты снимаешь квартиру на десять комнат, а живёшь в одной. "
    "Так большинство команд платят за Kubernetes каждый месяц. "
    "Разработчики ставят requests с запасом, на всякий случай. "
    "Сначала сними реальное потребление за две недели. "
    "Потом урежь requests по фактическим цифрам. "
    "И выкатывай изменения через GitOps по десять процентов в день. "
    "Забирай готовые конфиги в нашем Telegram."
)
PLAN = ScenePlan(
    hook_big="35%",
    hook_small="счёта за Kubernetes уходит в пустоту",
    problem_before="Ставим requests с запасом",
    problem_after="Платим за простаивающие ядра",
    steps=["Сними метрики за 14 дней", "Урежь requests по факту", "Выкатывай через GitOps"],
    stock_queries=[],
)
if "--claim" in sys.argv:   # раскладка «спор»: миф зачёркивается, появляется правда
    PLAN.layout = "claim"
    PLAN.myth = "Запас в requests — это надёжность"
    PLAN.truth = "Это счёт за пустые ядра"
if "--flow" in sys.argv:    # схема причин и следствий вместо карточек «ошибка → итог»
    PLAN.flow = ["Requests с запасом", "Ядра простаивают", "Счёт растёт", "Бюджет в минус"]
    PLAN.flow_icons = ["code", "server", "money", "bolt"]
CODE = ('sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)\n'
        '  / sum(kube_pod_container_resource_requests) by (pod)\n'
        '> 0.8')


def fake_words(text: str) -> list[Word]:
    words, t = [], 0.3
    for w in text.split():
        d = 0.11 + 0.042 * len(w)
        words.append(Word(w, t, t + d))
        t += d + 0.05
    return words


async def main() -> None:
    from playwright.async_api import async_playwright

    words = fake_words(SCRIPT)
    config = video.build_config(words, PLAN, "Как срезать чек на Kubernetes", CODE, "promql")
    print("сцены:", config["scenes"], "длительность:", config["duration"])

    args = [float(a) for a in sys.argv[1:] if a.replace(".", "").isdigit()]
    if not args:
        args = [(s + e) / 2 for s, e in config["scenes"].values()]

    outdir = settings.media_dir / "preview"
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("*.png"):
        old.unlink()

    launch = {"args": ["--no-sandbox", "--disable-dev-shm-usage", "--force-color-profile=srgb",
                       "--disable-lcd-text", "--font-render-hinting=none", "--hide-scrollbars", "--disable-gpu"]}
    if settings.chromium_path and Path(settings.chromium_path).exists():
        launch["executable_path"] = settings.chromium_path

    shots: list[Path] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch)
        page = await browser.new_page(viewport={"width": video.W, "height": video.H}, device_scale_factor=1)
        await page.set_content(video._html(config), wait_until="load")
        await page.wait_for_function("window.layoutDone === true", timeout=30000)
        for t in args:
            await page.evaluate("t => window.seek(t)", t)
            p = outdir / f"t{t:05.1f}.png"
            # на прозрачном фоне кадр не посмотреть — подкладываем тёмный, как ffmpeg в проде
            await page.evaluate("() => document.body.style.background = '#05070a'")
            await page.screenshot(path=str(p), type="png")
            await page.evaluate("() => document.body.style.background = 'transparent'")
            shots.append(p)
            print("кадр", t, "->", p)
        await browser.close()

    try:
        subprocess.run(
            [settings.ffmpeg_bin, "-y", "-loglevel", "error",
             *sum([["-i", str(p)] for p in shots], []),
             "-filter_complex", (f"hstack=inputs={len(shots)}," if len(shots) > 1 else "") + f"scale={270 * len(shots)}:-1",
             str(outdir / "sheet.png")],
            check=True,
        )
        print("склейка:", outdir / "sheet.png")
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print("склейку не сделал (нет ffmpeg?):", e)


asyncio.run(main())
