FROM python:3.13-slim

# ffmpeg — склейка видео и звука; deno — JS-рантайм, который нужен yt-dlp для YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# yt-dlp ставится свежий при каждой сборке (сайты часто ломают старые версии):
# docker compose build --no-cache
RUN pip install --no-cache-dir -r requirements.txt "yt-dlp[default]"

COPY bot.py .

# data/ — файл сессии Telethon, монтируется как том
RUN useradd --create-home bot && mkdir -p /app/data && chown bot:bot /app/data
USER bot

CMD ["python", "bot.py"]
