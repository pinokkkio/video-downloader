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

COPY docker/yt-dlp-proxy /usr/local/bin/yt-dlp-proxy
COPY bot.py .

RUN useradd --create-home bot
USER bot

CMD ["python", "bot.py"]
