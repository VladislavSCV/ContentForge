from functools import cached_property
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    telegram_bot_token: str
    admin_ids: str  # "123,456" — кто может пользоваться ботом
    tg_channel_id: str = ""  # @канал или id; куда публиковать (бот должен быть админом канала)
    tg_channel_link: str = ""

    # --- LLM ---
    llm_provider: str = "yandex"  # yandex | openai | deepseek | anthropic
    llm_model: str = "yandexgpt/latest"
    llm_max_tokens: int = 4000
    # Yandex AI Studio: API-ключ сервисного аккаунта и ID каталога
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    # OpenAI-совместимый шлюз для openai/deepseek, например https://api.deepseek.com/v1
    llm_base_url: str = ""
    llm_api_key: str = ""
    anthropic_api_key: str = ""

    # --- TTS: yandex (SpeechKit, тот же YANDEX_API_KEY) | elevenlabs ---
    tts_provider: str = "yandex"
    # SpeechKit v3. Мужские: anton, kirill, alexander, ermil, zahar, filipp; женские: dasha, alena, jane, masha, lera
    yandex_tts_voice: str = "alexander"
    yandex_tts_role: str = "good"  # интонация: good (дружелюбно), neutral, strict…; пусто — по умолчанию голоса
    tts_dynamic: bool = True  # длинный ролик: скорость и высота голоса меняются по смыслу (tts.plan_delivery)
    yandex_tts_speed: float = 1.0 # 0.1–3.0; ускорение делает речь заметно синтетичнее

    # --- ElevenLabs ---
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "pNInz6obpgDQGcFmaJgB"  # Adam — мужской голос, поменяй на свой
    elevenlabs_model: str = "eleven_multilingual_v2"

    # --- Cloudflare Worker для сервера в РФ (cloudflare/worker.js) ---
    # Через него идут Telegram Bot API, загрузка на YouTube и страницы LinkedIn. Пусто — всё напрямую.
    telegram_api_url: str = ""
    cf_proxy_secret: str = ""  # = PROXY_SECRET в Worker'е; нужен для маршрутов YouTube и LinkedIn

    # --- YouTube (scripts/youtube_auth.py) ---
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    # второй токен, только на чтение статистики (youtube.readonly + yt-analytics.readonly); без него итоги недели без YouTube
    youtube_stats_refresh_token: str = ""
    youtube_privacy: str = "public"  # public | unlisted | private
    youtube_category_id: str = "28"  # Science & Technology

    # --- Стоковые видео для фона Shorts (pixabay.com/api/docs, бесплатно). Пусто — анимированный градиент ---
    pixabay_api_key: str = ""

    # --- Рендер ---
    data_dir: Path = Path("data")
    backgrounds_dir: Path = Path("assets/backgrounds")
    fonts_dir: Path = Path("assets/fonts")
    pronunciation_file: Path = Path("assets/pronunciation.txt")  # как читать трудные слова: ударения, написание
    mascot_dir: Path = Path("assets/mascot")  # mascot.png (+ необязательный mascot_talk.png с открытым ртом)
    # Профиль контента = папка profiles/<имя>/ (промпты, темы, ленты, флаги). PROFILE=general; прежнее имя CONTENT_PROFILE работает.
    # Ленты берутся из профиля; SOURCES_FILE в .env их переопределяет.
    content_profile: str = Field("engineering", validation_alias=AliasChoices("profile", "content_profile"))
    profiles_dir: Path | None = None  # папка с профилями; по умолчанию profiles/ рядом с app/
    # Площадки и возможности (app/features.py): * — всё; иначе список, например FEATURES=week,reddit
    features: str = "*"
    tg_rubrics: bool = True  # первая строка Telegram-поста — рубрика (столп бренда); TG_RUBRICS=false отключает
    story_shorts: bool = True  # второй формат Shorts «история с героем» для материалов с героем, годом и поворотом
    ffmpeg_bin: str = "ffmpeg"
    # тихие звуковые акценты (whoosh, удар, тик) под голосом; SFX_ENABLED=false — ролик только с голосом
    sfx_enabled: bool = True
    # фоновая музыка (app/music.py, синтез без файлов), приглушается под голос; MUSIC_ENABLED=false — без музыки
    music_enabled: bool = True
    # пик музыки как доля от пика голоса
    music_level: float = 0.30
    # стиль музыки: calm (мягкий), tense (тёмный, для сбоев и взломов), drive (бодрый); образцы — scripts/music_samples.py
    music_style: str = "calm"
    # Chromium для покадрового рендера вёрстки; пусто — тот, что скачал Playwright
    chromium_path: str = ""
    # сколько вкладок Chromium снимают кадры параллельно; по числу ядер сервера
    render_workers: int = 2
    # как канал подписан в кадре
    brand_name: str = "ContentOps"  # название бренда на экране ролика; задаётся в .env
    brand_handle: str = ""  # @канал; ищется поиском Telegram; ссылки в Shorts некликабельны
    # призыв в конце ролика: subscribe (подписка на YouTube) | telegram (увести в канал)
    cta_mode: str = "subscribe"
    # --- темы и очередь публикаций ---
    sources_file: Path | None = None  # RSS/Atom-ленты; пусто — sources_file профиля
    topics_count: int = 3  # сколько тем присылать за раз
    linkedin_lang: str = "en"  # язык поста для LinkedIn: en — международные офферы, ru — как раньше
    reminders: bool = True  # бот сам напоминает про ручные шаги (app/reminders.py); false — молчать
    topics_llm: bool = True  # второй этап отбора: модель оценивает финалистов эвристики (app/topic_rank.py)
    topics_time: str = "09:00"  # когда присылать подборку каждый день (местное время)
    digest_time: str = "18:00"  # итоги недели приходят по воскресеньям в это время
    # у каналов свои графики. Telegram: по дням недели (1 = понедельник), одно окно в день.
    # YouTube: окна каждый день. Шортс выходит не раньше поста в канале: ролик зовёт «в Telegram», и пост должен уже висеть.
    tg_days: str = "1,3,5"
    tg_time: str = "12:00"
    publish_slots: str = "19:00"  # окна YouTube, через запятую; один ролик на окно
    # адрес для пинга «бот жив» (например, healthchecks.io): если пинги прекратятся, сервис сам оповестит
    heartbeat_url: str = ""
    # смещение местного времени от UTC в часах; без зон, чтобы не зависеть от tzdata в образе
    schedule_utc_offset: int = 3
    # true: ролик без замечаний автопроверки сам встаёт в очередь; с замечаниями ждёт ручной проверки
    auto_queue: bool = False

    @field_validator("features")
    @classmethod
    def _check_features(cls, v: str) -> str:
        from .features import parse   # ошибка в FEATURES видна при старте, а не при первой команде
        parse(v)
        return v

    @cached_property
    def admin_id_set(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x}

    @property
    def tts_enabled(self) -> bool:
        if self.tts_provider == "elevenlabs":
            return bool(self.elevenlabs_api_key)
        return bool(self.yandex_api_key and self.yandex_folder_id)

    @property
    def youtube_stats_enabled(self) -> bool:
        return bool(self.youtube_client_id and self.youtube_client_secret and self.youtube_stats_refresh_token)

    @property
    def youtube_enabled(self) -> bool:
        return bool(self.youtube_client_id and self.youtube_client_secret and self.youtube_refresh_token)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "contentops.sqlite3"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


settings = Settings()
