FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
# запас по таймауту и повторам: сборка часто идёт через медленный канал
RUN pip install --no-cache-dir --timeout 180 --retries 10 -r requirements.txt

# Chromium рендерит кадры Shorts из вёрстки. Браузер кладём в общий путь и открываем на чтение,
# чтобы контейнер можно было запускать и не от root (docker run -u ...).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY assets ./assets
COPY profiles ./profiles

CMD ["python", "-m", "app.main"]
