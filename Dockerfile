FROM python:3.13-slim

# ffmpeg — склейка видео и звука; deno — JS-рантайм, который нужен yt-dlp для YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# yt-dlp ставится свежий при каждой сборке (сайты часто ломают старые версии)
RUN pip install --no-cache-dir -r requirements.txt "yt-dlp[default]"

COPY bot.py .

# Файл сессии Telethon хранится в /data — это постоянное хранилище и на
# Amvera (persistenceMount), и в docker compose (том session).
# Процесс работает от root: права на хранилище, которое монтирует хостинг,
# заранее неизвестны, а пользователю без прав не удастся записать сессию.
ENV SESSION=/data/bot
RUN mkdir -p /data

CMD ["python", "bot.py"]
