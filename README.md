# ContentOps Engine — LinkedIn ➔ Telegram + YouTube Shorts

Пост в LinkedIn «стрельнул» → кидаешь ссылку (или текст) своему админ-боту в Telegram →
бот через YandexGPT делает пост для канала и сценарий Shorts → Yandex SpeechKit озвучивает →
вёрстка в headless Chromium собирает вертикальный ролик → тебе приходит карточка модерации →
по кнопке пост уходит в канал, а ролик на YouTube; текст комментария со ссылкой бот присылает тебе — вставить и закрепить вручную.

```
ты ──ссылка/текст──▶ Admin Bot ──▶ YandexGPT (JSON: TG-пост + Shorts)
                                    ├─▶ Telegram HTML (санитайзинг, код, CTA, хештеги)
                                    └─▶ SpeechKit (аудио + таймкоды) ─▶ Chromium + ffmpeg (1080×1920)
                     ◀── карточка: превью поста + видео + [✅ везде] [🚀 TG] [🎬 YT] [✏️] [🔁] [🗑]
                     ──▶ Telegram канал  /  YouTube videos.insert + commentThreads.insert
```

## Чем реализация отличается от ТЗ (и почему)

| В ТЗ | Сделано | Почему |
|---|---|---|
| n8n / Make оркестратор | Python-сервис (`app/pipeline.py`) | Одна сущность вместо бот+n8n+webhook, логику модерации/редактирования проще держать в коде, легко тестировать. |
| Airtable / Supabase | SQLite (`data/contentops.sqlite3`) | Нулевая настройка. Статусы те же: `new → generating → review → published` (+ `failed`). |
| Claude 3.5 Sonnet | **YandexGPT** по умолчанию (`LLM_PROVIDER=yandex`), переключается на DeepSeek/OpenAI-совместимый шлюз или Claude одной строкой в `.env` | Доступ из РФ. Ответ — JSON по схеме, проверяется Pydantic; при ошибке модель получает текст ошибки и переделывает (до 3 попыток). Типовые косяки слабой модели правятся кодом: `null` вместо кода, нет жирного заголовка, дубли CTA/хештегов, нет ссылки на канал; слишком короткий текст диктора дописывается отдельным коротким запросом. |
| ElevenLabs | **Yandex SpeechKit** по умолчанию (`TTS_PROVIDER=yandex`), ElevenLabs — опция | Тот же ключ Яндекса, оплата из РФ. SpeechKit не отдаёт таймкоды слов, поэтому озвучка идёт по предложениям: их начало известно точно (проверено по паузам в аудио), слова внутри раскладываются по длине. Голос и скорость — `YANDEX_TTS_VOICE` / `YANDEX_TTS_SPEED`. |
| Submagic / Remotion | Своя композиция на HTML/CSS, кадры снимает headless Chromium ([app/render/composition.html](app/render/composition.html)) | Идея Remotion, но без его лицензии и без Node. Браузер даёт настоящую типографику (Inter), тени, маски и пружинные анимации: сцены хук → проблема → шаги → канал, «печатающийся» терминал с подсветкой кода, субтитры по словам, полоса прогресса. ffmpeg подкладывает стоковое видео и сводит со звуком. |
| Закреплённый комментарий YouTube | Готовый текст комментария приходит в Telegram после загрузки | **Безопасность:** бот получает только право `youtube.upload` — даже при утечке токена им нельзя удалить или изменить видео. Комментарии требуют полного доступа к каналу, а закрепить через API всё равно нельзя, поэтому вставить + закрепить — вручную, 10 секунд. |

## Быстрый старт

1. **Telegram.** Создай бота у @BotFather, добавь его **админом** в канал с правом публикации. Свой user id узнай у @userinfobot.
2. **Ключи.** В `.env` заполни: `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, `TG_CHANNEL_ID`, свой Telegram. YandexGPT и озвучка SpeechKit работают от уже прописанного `YANDEX_API_KEY`.
   Проверить без Telegram: `docker compose run --rm contentops python scripts/try_llm.py --video` → тексты в консоли и ролик в `data/media/try/shorts_render.mp4` (свой пост: `... try_llm.py мой_пост.txt --video`).
   **Модель:** `yandexgpt-5-lite/latest` справляется, но пишет суховато и без `<code>`; `LLM_MODEL=yandexgpt/latest` (Pro) заметно лучше — сама ставит разметку, аналогии и подробный сценарий. Стоит в разы дороже Lite, но на один пост это копейки.
3. **YouTube** (можно позже — без него работает всё, кроме загрузки):
   - Google Cloud Console → проект → включи *YouTube Data API v3*;
   - OAuth consent screen → External → добавь свой аккаунт в *Test users*;
   - Credentials → OAuth client ID → *Desktop app* → скачай как `client_secret.json`;
   - на машине с браузером: `pip install google-auth-oauthlib && python scripts/youtube_auth.py` → три строки в `.env`.
4. **Фоны для Shorts.** Положи 3–10 вертикальных или любых роликов (запись терминала, Grafana, IDE) в `assets/backgrounds/`. Берётся случайный, кадрируется в 9:16 и зацикливается. Если папка пустая — тёмный фон с кодом из поста.
5. **Запуск:**
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
6. Проверка рендера без ключей: `docker compose run --rm contentops python scripts/smoke_test.py` → `data/media/smoke/shorts_render.mp4`.

## Сервер в РФ: Telegram, YouTube и LinkedIn через Cloudflare Worker

С сервера в РФ недоступны `api.telegram.org` (Bot API), `linkedin.com` и может не работать `googleapis.com`.
Cloudflare при этом доступен. Поэтому между сервером и внешними API стоит небольшой Cloudflare Worker
([cloudflare/worker.js](cloudflare/worker.js)): принимает запрос и пересылает его дальше без изменений.
Отдельного сервера за границей нет. Worker работает на бесплатном тарифе Cloudflare.

```
Сервер в РФ (Docker)                 Cloudflare Worker (contentops-proxy)
бот · YandexGPT · SpeechKit   ──▶    /bot<токен>/…        ──▶ api.telegram.org      (только ALLOWED_BOT_ID)
TELEGRAM_API_URL = Worker            /g/oauth2/token|info ──▶ oauth2.googleapis.com ┐ нужен заголовок
CF_PROXY_SECRET  = PROXY_SECRET      /g/api/upload/…      ──▶ www.googleapis.com    ├ X-Proxy-Secret
                                     /li?url=…            ──▶ linkedin.com          ┘
```

Бот работает в режиме polling: сам запрашивает обновления у Telegram через Worker. Входящих соединений нет,
ни домен, ни HTTPS на сервере не нужны.

### Компоненты и где лежат настройки

| Что | Где | Зачем |
|---|---|---|
| Worker | dash.cloudflare.com → Workers & Pages → `contentops-proxy` | Пересылает запросы |
| Код Worker'а | [cloudflare/worker.js](cloudflare/worker.js) | Эталонная копия кода, вставленного в Cloudflare |
| Конфиг CLI-деплоя | [cloudflare/wrangler.toml](cloudflare/wrangler.toml) | Имя Worker'а и `ALLOWED_BOT_ID` для `wrangler deploy` |
| `ALLOWED_BOT_ID` | Worker → Settings → Variables (Text) | ID бота — цифры токена до двоеточия, не секрет |
| `PROXY_SECRET` | Worker → Settings → Variables (**Secret**) | Пускает к маршрутам YouTube и LinkedIn только наш бот |
| `TELEGRAM_API_URL`, `CF_PROXY_SECRET` | `.env` бота на сервере | Адрес Worker'а и тот же секрет |

Что делает Worker:
- `/bot<токен>/<метод>`, `/file/bot<токен>/…` — проверяет ID бота по `ALLOWED_BOT_ID` и пересылает в `api.telegram.org`. Чужой бот — 403.
- `/g/oauth2/token`, `/g/oauth2/tokeninfo`, `/g/api/upload/youtube/v3/videos` — только с верным `X-Proxy-Secret`, пересылает в Google. Другие пути Google — 404, так что даже с секретом через Worker нельзя ничего, кроме загрузки видео.
- `/li?url=…` — только с секретом и только `https://*.linkedin.com`, отдаёт публичную страницу поста.
- Всё остальное — 404.

### Как развернуть Worker

**Через дашборд:**
1. dash.cloudflare.com → Workers & Pages → Create → Worker → имя `contentops-proxy` → Deploy.
2. Edit code → вставить содержимое [cloudflare/worker.js](cloudflare/worker.js) → Deploy.
3. Settings → Variables and Secrets:
   - `ALLOWED_BOT_ID` (Text) — цифры токена бота до двоеточия;
   - `PROXY_SECRET` (Secret) — значение `CF_PROXY_SECRET` из `.env`.
4. Адрес Worker'а (`https://contentops-proxy.<аккаунт>.workers.dev`) — в `.env` как `TELEGRAM_API_URL`.

**Через CLI** (по желанию):
```bash
cd cloudflare
npx wrangler login
# ALLOWED_BOT_ID вписать в wrangler.toml
npx wrangler deploy
npx wrangler secret put PROXY_SECRET     # вставить CF_PROXY_SECRET из .env
```
Имя в `wrangler.toml` должно совпадать с Worker'ом в дашборде, иначе CLI создаст второй. Значение `ALLOWED_BOT_ID`
из файла при деплое перезапишет заданное в дашборде.

### Сервер

Любой VPS с Ubuntu 22.04/24.04 (Timeweb, Selectel, Yandex Cloud и т.п.), 2 vCPU / 2 ГБ RAM хватает.
```bash
scp scripts/server_setup.sh root@<IP>:/root/ && ssh root@<IP> bash /root/server_setup.sh   # docker + зеркала Docker Hub
scripts/deploy.sh root@<IP>                                                                # код + .env → сервер, сборка, запуск
```
Обновление после правок — снова `scripts/deploy.sh root@<IP>`.

### Как проверить, что всё работает

1. Worker пускает бота (с сервера):
   `curl https://contentops-proxy.<аккаунт>.workers.dev/bot<ТОКЕН>/getMe` → JSON с `"ok":true`. 403 — ID бота нет в `ALLOWED_BOT_ID`. 404 — опечатка в пути.
2. В боте команда **/health**: LLM, озвучка и строка «▶️ YouTube (через Cloudflare Worker) ✅ · только загрузка 🔒».
3. Логи: `ssh root@<IP> 'cd /opt/contentops && docker compose logs --tail=30'` — норма: `Run polling for bot @…`.

### Частые проблемы

| Симптом | Причина | Что сделать |
|---|---|---|
| 403 в логах бота на Telegram-запросах | Бот не в `ALLOWED_BOT_ID` | Добавить ID бота в переменную Worker'а |
| YouTube: 403 Forbidden | `CF_PROXY_SECRET` ≠ `PROXY_SECRET` в Worker'е | Сверить значения |
| Cannot connect, таймауты | Сервер не достаёт Cloudflare или пустой `TELEGRAM_API_URL` | Проверить `.env`, `curl …/getMe` с сервера |
| 409 Conflict | Тот же бот запущен ещё где-то (например, локально) | Остановить вторую копию: у одного токена только один polling |
| `OAuth 400 invalid_grant` | Протух токен YouTube (приложение в Testing — 7 дней) | `python scripts/youtube_auth.py`, новый токен в `.env` |
| `docker compose build` висит на `python:3.12-slim` | Не сработали зеркала Docker Hub | Проверить `/etc/docker/daemon.json` |

### Безопасность

- Ключей Cloudflare на сервере и в репозитории нет. Worker управляется только через аккаунт Cloudflare.
- Токен бота и токен YouTube лежат только в `.env` на сервере. Они идут через Worker, поэтому Cloudflare технически их видит, как любой прокси. При подозрении на утечку: бот — `/revoke` в @BotFather, YouTube — https://myaccount.google.com/permissions → удалить доступ ContentOps.
- Токен YouTube имеет только право загрузки: удалить или изменить видео им нельзя.
- Worker не хранит данных и не пишет логов содержимого.

## Как пользоваться

- Пришли боту **ссылку на пост LinkedIn**. Если пост публичный — бот сам заберёт текст; если LinkedIn требует логин — попросит вставить текст.
- Или сразу **вставь/перешли текст** поста (можно вместе со ссылкой).
- Через ~1–2 минуты придёт карточка: превью поста (ровно как будет в канале), ролик с YouTube-метаданными и кнопки:
  - **✅ Опубликовать везде / 🚀 Только Telegram / 🎬 Только YouTube** — повторная публикация не задублирует уже опубликованное;
  - **✏️ Редактировать текст** — либо скопируй превью, поправь прямо в Telegram и пришли обратно (форматирование сохранится), либо напиши `ai: короче, добавь пример с kubectl`;
  - **🔁 Перегенерировать** — заново всё (текст + видео), можно с пожеланиями;
  - **🗑 Отклонить**.
- `/list` — последние посты, `/post 12` — снова открыть карточку.

## Важно знать заранее

- **YouTube API-проекты без аудита.** Видео, загруженные через непроверенный Google API-проект, YouTube принудительно делает *private*. Чтобы ролики сразу были публичными, пройди [YouTube API Services audit](https://support.google.com/youtube/contact/yt_api_form) (бесплатно, обычно 1–3 недели). До этого можно публиковать вручную из Studio одной кнопкой.
- **Квота YouTube:** 10 000 единиц в день, загрузка ≈ 1 600 → около 6 роликов в сутки. Хватает с запасом.
- **Refresh token** OAuth-клиента в статусе *Testing* живёт 7 дней. Переведи consent screen в *In production* (для личного использования верификация не нужна), чтобы токен не протухал.
- **Отправка видео в Telegram-бот** ограничена 50 МБ; 60-секундный ролик весит ~10–25 МБ.
- **Стоимость одного поста:** YandexGPT — 2–4 запроса по ~3–5 тыс. токенов; SpeechKit — ≈ 1 000 символов синтеза. Всё — рубли за пост.

## Структура

```
app/
  main.py        запуск бота (long polling), Bot API через TELEGRAM_API_URL
  bot.py         хендлеры, карточка модерации, кнопки, режимы редактирования, /health
  pipeline.py    оркестратор: генерация → медиа → публикация
  ai.py          LLM (YandexGPT / DeepSeek / Claude): JSON по схеме (schemas.py), автопочинка, правки
  prompts.py     системный промпт из ТЗ
  tg_format.py   сборка и санитайзинг HTML под Telegram
  linkedin.py    извлечение текста публичного поста по ссылке (через Worker из РФ)
  tts.py         озвучка: Yandex SpeechKit (по предложениям) или ElevenLabs → аудио + таймкоды слов
  video.py       сценарий кадров, покадровый рендер в Chromium, сборка ffmpeg 1080×1920
  render/composition.html  сама вёрстка ролика: сцены, анимации, субтитры, терминал
  youtube.py     загрузка Shorts (только право youtube.upload), напрямую или через Worker
  db.py          SQLite, статусы
cloudflare/
  worker.js      Cloudflare Worker: Telegram Bot API + загрузка YouTube + LinkedIn
  wrangler.toml  конфиг для деплоя через CLI (npx wrangler deploy)
scripts/
  youtube_auth.py  получить refresh token (только право загрузки)
  server_setup.sh  подготовка сервера в РФ (docker + зеркала)
  deploy.sh        выкладка на сервер
  smoke_test.py    офлайн-проверка форматирования и полного рендера
  preview_frames.py  снять отдельные кадры в PNG — быстрый просмотр дизайна
  try_llm.py       прогнать генерацию (и --video: озвучку + рендер) без Telegram
assets/backgrounds/  фоны для Shorts
assets/fonts/        Inter.ttf — шрифт роликов (обязателен); Mono.ttf — для кода, иначе DejaVu
```
