"""Офлайн-проверка без API-ключей: санитайзер Telegram-HTML и полный рендер тестового Shorts.

    docker compose run --rm contentops python scripts/smoke_test.py
Результат: data/media/smoke/shorts_render.mp4 — удобно подбирать фон и стиль.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS"):
    os.environ.setdefault(k, "1")
# тестовые данные ниже инженерные (ленты, пост): профиль берём engineering, если его не задали явно
if not (os.environ.get("PROFILE") or os.environ.get("CONTENT_PROFILE")):
    os.environ["PROFILE"] = "engineering"

from app import ai, video  # noqa: E402
from app.config import settings  # noqa: E402
from app.schemas import ContentPackage, ScenePlan, ShortsStream, TelegramStream  # noqa: E402
from app.tg_format import build_post, sanitize  # noqa: E402
from app.tts import Word  # noqa: E402

# --- санитайзер ---
assert sanitize("<h2>Title</h2><p>a<br>b</p><ul><li>x</li></ul>") == "<b>Title</b>\na\nb\n\n• x"
assert sanitize("<b>unclosed <i>nested") == "<b>unclosed <i>nested</i></b>"
assert sanitize("1 < 2 & <script>alert(1)</script>") == "1 &lt; 2 &amp; alert(1)"
assert sanitize('<a href="javascript:x">l</a>') == "l"
post = build_post(TelegramStream(
    formatted_text="<b>Режем requests</b>\n\nТекст", code_snippet='sum(rate(x{ns="a"}[5m])) > 0',
    code_language="PromQL", hashtags=["sre", "#k8s"], cta_text="🔥 Конфиг в закрепе",
))
assert '<pre><code class="language-promql">sum(rate(x{ns="a"}[5m])) &gt; 0</code></pre>' in post, post
assert post.endswith("#sre #k8s")
print("tg_format OK")

# --- вырезание выдуманных инструментов: в посте про Docker «кубернетес» взяться неоткуда ---
yt = ShortsStream(hook_0_3s="Образ был полтора гига.", problem_3_15s="Деплой занимал девять минут.",
                  solution_15_40s="Собери в два этапа. Ваш кубернетес-образ полегчает.", cta_40_60s="Клади только нужное.",
                  full_audio_script="", youtube_title="t", youtube_description="d", youtube_tags=[], first_comment="c")
pkg = ContentPackage(telegram_stream=TelegramStream(formatted_text="x", code_snippet="", code_language="",
                                                    hashtags=[], cta_text=""), youtube_shorts_stream=yt)
found = ai.strip_invented("We cut our Docker image from 1.4 GB to 85 MB", pkg)
assert found and "кубернетес" not in yt.solution_15_40s and "Собери в два этапа." in yt.solution_15_40s, (found, yt)
assert not ai.strip_invented("We moved Kubernetes to Docker", pkg)
print("strip_invented OK")

# YandexGPT отвергает схему с необязательными полями (400 «all fields must be required»)
for model in (ScenePlan, ContentPackage):
    def check(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                assert set(node["required"]) == set(node["properties"]), node
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)
    check(ai._inline_schema(model))
print("schema required OK")

# --- словарь произношений: правит только текст для синтезатора ---
import tempfile  # noqa: E402

from app import pronounce  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    dic = Path(tmp) / "p.txt"
    dic.write_text("замок = зам+ок  # ударение\nкластер* = кл+астер*\nпостгрес = постгр+эс\nнет без равно\n", encoding="utf-8")
    assert pronounce.apply("Замок в кластерах, а не в постгресе.", dic) == "зам+ок в кл+астерах, а не в постгресе.", \
        pronounce.apply("Замок в кластерах, а не в постгресе.", dic)
    assert pronounce.apply("Постгрес.", dic) == "постгр+эс."
    assert pronounce.apply("текст", Path(tmp) / "нет.txt") == "текст"
real_dict = settings.pronunciation_file
assert pronounce.apply("OpenShell в Kubernetes с Argo CD и PostgreSQL", real_dict) == \
    "оупенш+елл в куберн+этис с +арго си-ди и п+остгрес-кью-+эль", pronounce.apply("OpenShell в Kubernetes с Argo CD и PostgreSQL", real_dict)
assert pronounce.apply("GitHub, Docker-образ, CICD и sql", real_dict) == "гитх+аб, докер-образ, си-ай си-ди и эс-кью-+эль"
assert pronounce.apply("Постгрес", real_dict) == "п+остгрес"            # старые, проверенные на слух правила не сломаны
# все ключи словаря корректны: нет пустых и дублей (дубль молча перекрывал бы вариант)
keys = [ln.split("=", 1)[0].strip().lower() for ln in real_dict.read_text(encoding="utf-8").splitlines()
        if "=" in ln.split("#", 1)[0]]
dups = {k for k in keys if keys.count(k) > 1}
assert not dups, dups
print("pronounce OK")

# --- версия: разбор файла, запасной вариант без файла ---
from datetime import timedelta  # noqa: E402

from app import version  # noqa: E402

info = version.parse("05.11 04:10|a1b2c3d4|203.0.113.10\n")
assert (info.built, info.digest, info.target, info.known) == ("05.11 04:10", "a1b2c3d4", "203.0.113.10", True), info
assert not version.parse("").known and not version.parse("мусор").known
assert version.uptime(version.STARTED + timedelta(hours=26, minutes=5)) == "1 д 2 ч 5 мин", version.uptime(version.STARTED + timedelta(hours=26, minutes=5))
assert "Запущен:" in version.describe()
print("version OK")

assert "деньги" in ai.yandex_hint(403) and "YANDEX_API_KEY" in ai.yandex_hint(401) and ai.yandex_hint(500) == ""
print("yandex hint OK")

# --- чистка речи: висящий предлог после вырезанных скобок и опечатка в термине ---
from app.pipeline import _scrub  # noqa: E402

assert _scrub("Создали составной индекс по (customer_id, created_at DESC). Пострес сразу находит строки.") == \
    "Создали составной индекс. Постгрес сразу находит строки.", _scrub("Создали составной индекс по (customer_id, created_at DESC). Пострес сразу находит строки.")
print("scrub OK")

# --- автопроверка качества: хороший пакет проходит, плохой ловится по каждому правилу ---
from app import quality  # noqa: E402

src = "We cut our Docker image from 1.4 GB to 85 MB. Deploy took 9 minutes."
good_parts = ["Образ Docker весил полтора гигабайта, а деплой занимал девять минут.",
              "Каждый раз ждёшь, как очередь в одну кассу, пока всё скачается и соберётся на сервере.",
              "Собери образ в два этапа. Сначала скомпилируй код, потом скопируй только готовый файл. "
              "Так в образ не попадает мусор сборки, и он весит в разы меньше.",
              "Клади в образ только то, что нужно для запуска."]
good = ContentPackage(
    telegram_stream=TelegramStream(formatted_text="<b>Образ Docker 1.4 GB → 85 MB</b>\n\n" + "Многоэтапная сборка. " * 30,
                                   code_snippet="FROM golang:1.22 AS build\nRUN go build -o /app",
                                   code_language="dockerfile", hashtags=[], cta_text=""),
    youtube_shorts_stream=ShortsStream(
        hook_0_3s=good_parts[0], problem_3_15s=good_parts[1], solution_15_40s=good_parts[2],
        cta_40_60s=good_parts[3], full_audio_script=" ".join(good_parts), youtube_title="Образ Docker похудел #shorts",
        youtube_description=f"Конфиги: {settings.tg_channel_link} #shorts", youtube_tags=[], first_comment="c"))
assert quality.auto_ok(quality.check(src, good, good_parts)), quality.summary(quality.check(src, good, good_parts))

bad = good.model_copy(deep=True)
bad.telegram_stream.formatted_text = "<b>Kubernetes сэкономил 73% бюджета</b>\n\nкороткий"
bad.telegram_stream.code_snippet = "print((1, 2"
bad_parts = ["Привет, друзья!", "Мы использовали Kubernetes и GitOps.", "CONCURRENTLY.", "Подписывайся на телеграм."]
msgs = [i.text for i in quality.check(src, bad, bad_parts, has_video=False)]
for needle in ("короткий", "обрубленная", "приветствия", "нет в исходнике: kubernetes", "цифры, которых нет",
               "трудные английские", "не закрыты скобки", "видео не собралось"):
    assert any(needle in m for m in msgs), (needle, msgs)
assert not quality.auto_ok(quality.check(src, bad, bad_parts))
vy = good.model_copy(deep=True)
vy.youtube_shorts_stream.first_comment = "Узнайте больше в нашем канале"
assert any("«вы»" in i.text for i in quality.check(src, vy, good_parts)), quality.check(src, vy, good_parts)
assert not any("«вы»" in i.text for i in quality.check(src, good, good_parts))
print("quality OK")

# --- редактор Telegram-поста: правку берём, только если она не потеряла суть и не выдумала фактов ---
old_tg = TelegramStream(formatted_text="<b>Заголовок про Docker</b>\n\n" + "Образ стал меньше на 85 MB. " * 20, code_snippet="",
                        code_language="", hashtags=[], cta_text="Читай дальше")
def edited(**kw):
    d = old_tg.model_copy(deep=True)
    for k, v in kw.items():
        setattr(d, k, v)
    return d
assert ai._accept_review(old_tg, edited(formatted_text=old_tg.formatted_text.replace("стал", "станет")), src)
assert not ai._accept_review(old_tg, edited(formatted_text="<b>Коротко</b>"), src)                       # потеряна суть
assert not ai._accept_review(old_tg, edited(formatted_text=old_tg.formatted_text + " Это даёт 73% выигрыша."), src)   # новая цифра
assert not ai._accept_review(old_tg, edited(cta_text="Попробуй Kubernetes"), src)                       # новый инструмент
print("tg review OK")

# --- цифра хука на весь экран должна быть из материала ---
import asyncio as _aio  # noqa: E402


async def _fake_plan(prompt, output_format, system=None, max_tokens=None):
    return ScenePlan(hook_big="50%", hook_small="команд не измеряют", problem_before="a", problem_after="b",
                     steps=["Измеряй"], stock_queries=["server room"])

_orig_generate = ai._generate
ai._generate = _fake_plan
try:
    pkg_hook = good.model_copy(deep=True)
    pkg_hook.youtube_shorts_stream.youtube_title = "Как измерить успех платформы #shorts"
    plan_bad = _aio.run(ai.scene_plan("Most platform teams cannot prove their platform works.", pkg_hook))
    assert plan_bad.hook_big != "50%" and plan_bad.hook_small == "", plan_bad.hook_big          # «50%» в материале нет
    plan_ok = _aio.run(ai.scene_plan("Half of teams, 50% in total, cannot prove it.", pkg_hook))
    assert plan_ok.hook_big == "50%", plan_ok.hook_big                                          # здесь цифра есть — остаётся
finally:
    ai._generate = _orig_generate
print("hook number OK")

# --- LinkedIn: пост автора, который стрелял, проходит проверку; выдумки и ссылки ловятся ---
from app import longform, reddit as _reddit, topic_rank as _tr  # noqa: E402

longform.settings.linkedin_lang = "ru"    # проверки ниже русские; английские — в разделе «английский LinkedIn»
for model in (longform.LinkedInPost, longform.HabrPlan, longform.HabrSection, longform.DevtoMeta, longform.DevtoPart,
              longform.DevtoSection, _reddit.RedditReply, _tr.Verdicts):
    def check_req(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                assert set(node["required"]) == set(node["properties"]), node
            for v in node.values():
                check_req(v)
        elif isinstance(node, list):
            for v in node:
                check_req(v)
    check_req(ai._inline_schema(model))
_sample = Path(__file__).resolve().parent.parent / "assets/linkedin_examples/02_kafka_monolith.txt"
if _sample.exists():   # образцы постов личные и в репозиторий не входят; проверка идёт, если положить свой удачный пост под этим именем
    best = _sample.read_text(encoding="utf-8")
    ok = longform.check_linkedin(best, longform.LinkedInPost(text=best, first_comment="x"))
    assert not ok, ok
else:
    print("linkedin sample: пропущено (нет assets/linkedin_examples/02_kafka_monolith.txt)")
bad_li = longform.LinkedInPost(text="Привет! Мы сэкономили 73 000 000 рублей на Kubernetes https://x.io", first_comment="x")
msgs = longform.check_linkedin("Пост про Docker", bad_li)
for needle in ("длина", "нет вопроса", "ссылка в тексте", "цифры, которых нет", "kubernetes"):
    assert any(needle in m for m in msgs), (needle, msgs)
assert not longform.EXAMPLES_DIR.exists() or not list(longform.EXAMPLES_DIR.glob('*.txt')) or len(longform._examples()) > 1000   # образцы необязательны
print("linkedin OK")

# --- аналитика: разбор выгрузки LinkedIn и сводка недели ---
from datetime import datetime, timezone  # noqa: E402

from app import analytics  # noqa: E402

xlsx = Path(__file__).resolve().parent.parent / "PostPerformance_2025-11-05_2026-11-04.xlsx"
assert analytics.parse_linkedin_xlsx(b"not a zip") == []
if xlsx.exists():
    li_rows = analytics.parse_linkedin_xlsx(xlsx.read_bytes())
    assert len(li_rows) == 6 and max(r["impressions"] for r in li_rows) == 46743, li_rows
    best_row = max(li_rows, key=lambda r: r["impressions"])
    assert "главная проблема ai" in analytics.title_of(best_row["url"]), analytics.title_of(best_row["url"])
    for r in li_rows:
        r["imported_at"] = "2026-11-04T09:00:00+00:00"
    digest_text = analytics.build_digest([], li_rows, datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc))
    assert "46743" in digest_text and "Выпуск:" in digest_text and "слабее всех: 1239" in digest_text, digest_text
    assert "LinkedIn: данных нет" in analytics.build_digest([], [], datetime(2026, 11, 8, tzinfo=timezone.utc))
    print("analytics OK")

yt_text = analytics.build_digest([], [], datetime(2026, 11, 8, tzinfo=timezone.utc),
                                 yt=[{"title": "Образ Docker", "views": 1200, "likes": 40, "comments": 3, "avg_pct": 71.0, "subs": 5},
                                     {"title": "Kafka не нужна", "views": 300, "likes": 9, "comments": 0}])
assert "просмотров: 1500" in yt_text and "средний процент досмотра: 71%" in yt_text and "+5" in yt_text, yt_text
assert "YOUTUBE_STATS_REFRESH_TOKEN" in analytics.build_digest([], [], datetime(2026, 11, 8, tzinfo=timezone.utc))
print("youtube digest OK")

# --- строка «Источник»: только для тем из лент, один раз ---
from app import pipeline  # noqa: E402
from app.db import Post  # noqa: E402

p_feed = Post(1, "", "", "review", 1, None, "t", None, None, None, None, None, None, credit="blog.example.com\nhttps://blog.example.com/a?x=1&y=2")
p_own = Post(2, "", "", "review", 1, None, "t", None, None, None, None, None, None)
once = pipeline.with_credit("<b>Пост</b>", p_feed)
assert once.endswith('Источник: <a href="https://blog.example.com/a?x=1&amp;y=2">blog.example.com</a>'), once
assert pipeline.with_credit(once, p_feed) == once and pipeline.with_credit("<b>Пост</b>", p_own) == "<b>Пост</b>"
print("credit OK")

# --- темы: разбор RSS и Atom, оценка, отсев воды и дублей ---
from datetime import datetime, timedelta, timezone  # noqa: E402

from app import scheduler, topics  # noqa: E402

now = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
rss = f"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>How we cut our Kubernetes bill by 40%</title><link>https://a.example/1</link>
<description>&lt;p&gt;Postmortem-style story about rightsizing requests on EKS and what we learned along the way.&lt;/p&gt;</description>
<pubDate>{(now - timedelta(hours=5)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
<item><title>Join our webinar: register now</title><link>https://a.example/2</link><description>Kubernetes webinar</description></item>
</channel></rss>""".encode()
atom = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>How we cut Kubernetes bill by 40 percent</title>
<link href="https://b.example/1"/><summary>Same story as above retold with enough words to pass the length check easily.</summary>
<updated>{(now - timedelta(hours=2)).isoformat()}</updated></entry>
<entry><title>vLLM inference: why we lost a GPU node</title><link href="https://b.example/2"/>
<summary>Incident review of a GPU outage in a model serving cluster with root cause and the fix we applied.</summary>
<updated>{(now - timedelta(days=1)).isoformat()}</updated></entry></feed>""".encode()
cands = topics.parse_feed(rss, "a.example") + topics.parse_feed(atom, "b.example")
assert len(cands) == 4 and cands[0].published, cands
got = topics.pick(cands, {"https://a.example/1"}, 3, now)
urls = [c.url for c in got]
assert "https://a.example/2" not in urls, urls                  # вебинар отсеян
assert "https://b.example/2" in urls, urls                      # инцидент с GPU — берём
assert "https://a.example/1" not in urls, urls                  # уже предлагали
assert topics.parse_feed(b"<not xml", "x") == []
print("topics OK:", urls)

# --- столпы: по одной теме на столп, даже если инфраструктурные записи набрали больше ---
from app import topic_rank  # noqa: E402

_d = now - timedelta(hours=3)
_long = "Detailed write-up with numbers, root cause and the fix we applied afterwards in production. " * 2
pc = [topics.Candidate("Postmortem: 40% of pods lost after node drain", "https://i.example/1", _long, "i1", _d),
      topics.Candidate("Incident review: SLO burn after rollback", "https://i.example/2", _long, "i2", _d),
      topics.Candidate("Layoffs hit 30% of staff engineer roles: hiring market in 2026", "https://m.example/1", _long, "m1", _d),
      topics.Candidate("Why AI code review with Copilot is a bottleneck", "https://a.example/9", _long, "a1", _d)]
picked = topics.pick(pc, set(), 3, now)
assert {c.pillar for c in picked} == {"infra", "market", "ai"}, [(c.pillar, c.title) for c in picked]
assert len(topics.pick(pc, set(), 3, now, by_pillar=False)) == 3
assert "hiring" not in topics._PILLAR_AI and topics.pillar_of(topics.Candidate("Candidates and war stories", "u", "", "s", None)) == "infra"
assert topics._hn_signal("Points: 320 | # Comments: 150") == 3 and topics._hn_signal("no signal") == 0
# модель: поправка ограничена, мусор в ответе не ломает
assert topic_rank.bonus(topic_rank.Verdict(n=0, position=99, evidence=99, retelling=-5)) == topic_rank.MAX_BONUS
assert topic_rank.bonus(topic_rank.Verdict(n=0, position=0, evidence=0, retelling=3)) == -3.0
print("pillars OK")

# --- английский LinkedIn, dev.to, Reddit ---
import re  # noqa: E402

from app import longform, reddit, tells  # noqa: E402
from app.config import settings as _st  # noqa: E402

_st.linkedin_lang = "en"
en_good = ("Your service at 100 RPS does not need Kafka.\n\nMost teams add a queue because it feels safe, and then spend months "
           "running a cluster they never needed. A single Postgres table with SKIP LOCKED covers this load.\n\n"
           "What actually happens:\n1. Ops cost grows first.\n2. Latency gets worse.\n3. Nobody owns the cluster.\n\n"
           "What to do:\n- Stay on one table until 1000 RPS.\n- Add a broker only when two teams need replay.\n\n"
           "A queue you cannot explain is a liability.\n\nHave you seen this on your team? What was the tipping point?") * 1
en_good = en_good + "\n\n" + "Short extra paragraph to reach the minimum length of the post for the check. " * 8
assert not [i for i in longform.check_linkedin("100 RPS Postgres Kafka 1000", longform.LinkedInPost(text=en_good, first_comment="x"))
            if "кириллица" in i or "тезис" in i], "хороший английский пост отвергнут"
bad = "In today's fast-paced world, let's dive in. Привет коллеги, это пост.\n\nIt is not a tool, but a habit?"
bad_issues = longform.check_linkedin("", longform.LinkedInPost(text=bad, first_comment="x"))
assert any("кириллица" in i for i in bad_issues) and any("stock" in i for i in bad_issues), bad_issues
assert tells.ai_tells_en("It is not speed, but cost. This is not luck, but habit. It is not a bug, but a feature.") != []
assert tells.ai_tells_en("It is not speed, but cost. The rest is plain.") == []        # один раз — приём разрешён
_st.linkedin_lang = "ru"
art = "# Title\n\nLead text.\n\n## One\n\nBody one.\n\n## Two\n\nBody two with ```code```.\n\n---\nБольше — в Telegram: x"
t, lead, secs, tail = longform.split_article(art)
assert t == "Title" and lead == "Lead text." and [h for h, _ in secs] == ["One", "Two"] and tail.startswith("Больше"), (t, lead, secs, tail)
assert longform.clean_tags(["Kubernetes", "dev-ops", "kubernetes", "SRE!", "a", "b", "c"]) == ["kubernetes", "devops", "sre", "a"]
atom_r = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Is Kubernetes overkill for 3 services?</title>
<link href="https://www.reddit.com/r/devops/comments/x/"/><content>Question text about a small stack and cost.</content>
<updated>{now.isoformat()}</updated></entry></feed>""".encode()
rt = topics.parse_feed(atom_r, "r/devops")
assert len(rt) == 1 and rt[0].source == "r/devops" and "stack" in rt[0].summary, rt
_lab = longform._LABELS.sub(lambda m: "\0", "Closing line: smart is better.\nQuestion: how do you do it?")
assert re.sub(r"\0(\s*)(\w)", lambda m: m.group(2).upper(), _lab).replace("\0", "") == "Smart is better.\nHow do you do it?"
_st.linkedin_lang = "en"
pct_issues = longform.check_linkedin("cut costs by 68%", longform.LinkedInPost(text="We cut costs by 30% and 68%.", first_comment="x"))
assert any("процент" in i and "30" in i and "68" not in i for i in pct_issues), pct_issues
_st.linkedin_lang = "ru"
print("linkedin-en/devto/reddit OK")

# --- напоминания: календарь, окно, одно напоминание в сутки ---
from app import reminders  # noqa: E402

_mon = datetime(2026, 11, 9, 10, 30)          # понедельник
_r = reminders.RULES[0]
assert reminders.is_due(_r, _mon, None) and not reminders.is_due(_r, _mon, "2026-11-09")          # уже слали сегодня
assert not reminders.is_due(_r, _mon.replace(hour=9, minute=59), None)                            # рано
assert not reminders.is_due(_r, _mon.replace(hour=13, minute=1), None)                            # окно прошло: не шлём запоздавшее
assert not reminders.is_due(_r, _mon + timedelta(days=1), None)                                   # вторник
assert reminders.is_due(reminders.RULES[-1], datetime(2026, 11, 15, 11, 0), None)                 # 15-е число
assert len({r.key for r in reminders.RULES}) == len(reminders.RULES)
print("reminders OK")

# --- очередь: окна публикации, занятые окна пропускаются ---
scheduler.settings.publish_slots, scheduler.settings.schedule_utc_offset = "12:00,19:00", 3
t0 = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)            # 11:00 по Москве
s1 = scheduler.next_slot(set(), t0)
assert (s1.hour, s1.minute) == (12, 0) and s1.day == 4, s1
s2 = scheduler.next_slot({scheduler._iso(s1)}, t0)
assert (s2.hour, s2.day) == (19, 4), s2
s3 = scheduler.next_slot({scheduler._iso(s1), scheduler._iso(s2)}, t0)
assert (s3.hour, s3.day) == (12, 5), s3
late = scheduler.next_slot(set(), datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc))   # 19:00 МСК — окно уже «сейчас»
assert (late.hour, late.day) == (12, 5), late
print("scheduler OK")

# --- графики каналов: Telegram по Пн/Ср/Пт в 12:00, YouTube каждый день в 19:00 и не раньше поста в канале ---
scheduler.settings.tg_days, scheduler.settings.tg_time, scheduler.settings.publish_slots = "1,3,5", "12:00", "19:00"
tue = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)                 # вторник, 11:00 МСК
tg1 = scheduler.next_slot(set(), tue, [(12, 0)], {1, 3, 5})
assert (tg1.day, tg1.hour) == (4, 12), tg1                              # среда: вторник не публикуем
tg2 = scheduler.next_slot({scheduler._iso(tg1)}, tue, [(12, 0)], {1, 3, 5})
assert (tg2.day, tg2.hour) == (6, 12), tg2                              # четверг пропущен, пятница
yt1 = scheduler.next_slot(set(), tue, after=tg1 + scheduler.YT_AFTER_TG)
assert (yt1.day, yt1.hour) == (4, 19), yt1                              # вечер вторника раньше поста в канале — не берём
print("channel slots OK")

# --- обратная связь: пост LinkedIn находится по началу текста, поправки к темам считаются осторожно ---
from app import learning  # noqa: E402
from app.db import Post as _Post  # noqa: E402

draft = '{"text": "Главная проблема AI в разработке — это не увольнения инженеров. Это коллапс Code Review.", "first_comment": "x"}'
mine = _Post(7, "", "", "published", 1, None, "Copilot code review pull request", None, None, None, None, None, None, linkedin_json=draft)
other = _Post(8, "", "", "published", 1, None, "Про Kafka", None, None, None, None, None, None,
              linkedin_json='{"text": "Совсем другой пост про очереди сообщений и ничего больше.", "first_comment": "x"}')
if xlsx.exists():
    matched = learning.match_linkedin([other, mine], li_rows)
    assert list(matched) == [7] and matched[7]["impressions"] == 46743, matched
fake_perf = [(_Post(i, "", "", "published", 1, None, "copilot code review in pull request" if i < 6 else "plain text", None, None,
                    None, None, None, None), 3.0 if i < 6 else 1.0, {}) for i in range(24)]
adj = learning.compute(fake_perf)
assert adj.get("t4") == learning.CAP, adj                              # тема «AI в разработке» тянет вверх, поправка ограничена
assert learning.compute(fake_perf[:10]) == {}                           # данных мало — вес остаётся ручным
cand = topics.Candidate("Copilot code review", "https://x/1", "How we use copilot for code review", "x", None)
assert topics.score(cand, now, {"t4": 1.5}) - topics.score(cand, now) == 1.5
print("learning OK")

# --- оповещения: одинаковое сообщение не чаще заданного срока ---
import asyncio as _asyncio  # noqa: E402

from app import alerts  # noqa: E402


class _FakeBot:
    sent: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


alerts.init(_FakeBot())
assert _asyncio.run(alerts.notify("сбой", key="k")) is True
assert _asyncio.run(alerts.notify("сбой", key="k")) is False            # повтор подавлен
assert _asyncio.run(alerts.notify("другое", key="k2")) is True
assert len(_FakeBot.sent) == 2 and _FakeBot.sent[0][0] == sorted(settings.admin_id_set)[0], _FakeBot.sent
print("alerts OK")

# --- маскот: поза по смыслу фразы, без повторов подряд ---
from app import mascot  # noqa: E402

phrases = [(0.0, "Почему сервис падает?"), (3.0, "Мы потеряли тридцать восемь тысяч долларов."),
           (6.0, "Сервис упал на сорок семь минут."), (9.0, "Создали составной индекс."),
           (12.0, "Сначала проверь план запроса."), (15.0, "Запрос ускорился в сорок раз."),
           (18.0, "Подпишись, чтобы не пропустить."), (20.0, "А конфиги ждут в телеграме.")]
mwords = []
for start, text in phrases:
    for i, tok in enumerate(text.split()):
        mwords.append(Word(tok, start + i * 0.25, start + i * 0.25 + 0.2))
mscenes = {"hook": (0, 3), "problem": (3, 9), "solution": (9, 15), "takeaway": (15, 18), "cta": (18, 23)}
avail = set(mascot._needed())
tl = mascot.timeline(mwords, mscenes, avail)
at = lambda t: [p for ts, p in tl if ts <= t][-1]  # noqa: E731
assert at(3.1) == "13", tl            # деньги → пачка купюр
assert at(6.1) in ("10", "12", "04"), tl   # сбой → фейспалм/вздох/чешет голову
assert at(9.1) == "11", tl            # индекс → ноутбук
assert at(18.1) in ("14", "17", "01"), tl  # призыв → сердечко
assert all(a[1] != b[1] for a, b in zip(tl, tl[1:])), tl  # одинаковая поза подряд — не смена
print("mascot OK:", [p for _, p in tl])

# --- рендер ---
script = ("Представь, что ты платишь за пустую квартиру. "
          "Так и большинство команд платят за Kubernetes каждый месяц. "
          "Сначала сними метрики за две недели. "
          "Потом урежь requests по реальному потреблению. "
          "И выкатывай через GitOps по десять процентов в день. "
          "Забирай конфиги в нашем Telegram.")
words, t = [], 0.3
for w in script.split():
    d = 0.11 + 0.042 * len(w)
    words.append(Word(w, t, t + d))
    t += d + 0.05

plan = ScenePlan(
    hook_big="35%", hook_small="счёта за Kubernetes уходит в пустоту",
    problem_before="Ставим requests с запасом", problem_after="Платим за простаивающие ядра",
    steps=["Сними метрики за 14 дней", "Урежь requests по факту", "Выкатывай через GitOps"],
    stock_queries=[],
)
code = 'sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)\n  / sum(kube_pod_container_resource_requests) by (pod)\n> 0.8'
code_lang = "promql"
if "--docker" in sys.argv:  # Dockerfile: проверяем подсветку ключевых слов
    code = "FROM golang:1.22 AS build\nRUN go build -o /app\n\nFROM gcr.io/distroless/static\nCOPY --from=build /app /app"
    code_lang = "dockerfile"
if "--sql" in sys.argv:  # длинная однострочная команда и сцена без шагов — как при сбое плана сцен
    code = "CREATE INDEX CONCURRENTLY idx_orders_customer_created ON orders (customer_id, created_at DESC);"
    code_lang = "sql"
    plan.steps, plan.problem_before, plan.problem_after = [], "", ""
if "--nocode" in sys.argv:  # без кода шаги должны занять экран целиком
    code, code_lang = "", ""

workdir = settings.media_dir / "smoke"
workdir.mkdir(parents=True, exist_ok=True)
audio = workdir / "voice.wav"
subprocess.run([settings.ffmpeg_bin, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                f"sine=frequency=180:duration={t + 0.4:.2f}", str(audio)], check=True)

# синтетические «стоковые» клипы — проверяем нарезку фона по сценам и дрейф камеры
clips = []
for i, src in enumerate(("testsrc2", "smptebars")):
    clip = workdir / f"clip{i}.mp4"
    subprocess.run([settings.ffmpeg_bin, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"{src}=size=640x360:rate=30", "-t", "4", "-pix_fmt", "yuv420p", str(clip)], check=True)
    clips.append(clip)
if "--noclips" in sys.argv:  # как ролик без стока: проверяем анимированный фон вместо чёрного
    clips = []

# части речи: хук, проблема, решение (2 фразы), главная мысль, призыв
sentences = [x for x in script.replace("? ", ". ").split(". ") if x]
seg_words = [len(sentences[0].split()), len(sentences[1].split()),
             len(sentences[2].split()) + len(sentences[3].split()),
             len(sentences[4].split()), len(sentences[5].split())]
assert sum(seg_words) == len(words), (seg_words, len(words))
out = asyncio.run(video.render(workdir, audio, words, "Как срезать чек на Kubernetes",
                               code, plan, code_lang=code_lang, stock_clips=clips,
                               segment_words=seg_words, takeaway="И выкатывай через GitOps по десять процентов в день"))
probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True, check=True).stdout.strip()
assert probe.startswith("1080,1920"), probe
print(f"render OK: {out} ({out.stat().st_size // 1024} KB, {probe})")
