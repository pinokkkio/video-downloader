"""Telegram-бот: присылаешь ссылку — получаешь видео (не выше 720p).

Скачивание идёт через yt-dlp; прокси задаётся переменной YTDLP_PROXY.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Message, MessageEntity, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
YTDLP_CMD = os.environ.get("YTDLP_CMD", "yt-dlp")
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "")
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "720"))
# Ограничение Bot API на отправку файлов — 50 МБ. С локальным Bot API
# сервером (BOT_API_URL) можно поднять до 2000 МБ.
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))
BOT_API_URL = os.environ.get("BOT_API_URL", "")
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "600"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
ALLOWED_USERS = {
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USERS", "")) if x
}

# Если файл не влез в лимит — пробуем качество пониже.
FALLBACK_HEIGHTS = [h for h in (720, 480, 360, 240) if h <= MAX_HEIGHT] or [MAX_HEIGHT]
if FALLBACK_HEIGHTS[0] != MAX_HEIGHT:
    FALLBACK_HEIGHTS.insert(0, MAX_HEIGHT)

URL_RE = re.compile(r"https?://\S+")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)

# Типичные ошибки yt-dlp -> понятный пользователю текст.
YTDLP_ERRORS = [
    (("Unsupported URL", "is not a valid URL", "Unable to extract"),
     "Не удалось распознать ссылку: сайт не поддерживается или по ссылке нет видео."),
    (("There's no video", "No video formats found", "Requested format is not available"),
     "По ссылке не нашлось видео."),
    (("Video unavailable", "Private video", "This video is private", "HTTP Error 404",
      "has been removed", "does not exist"),
     "Видео недоступно: оно удалено, приватное или ссылка неверная."),
    (("Sign in to confirm", "login required", "Login required", "age-restricted",
      "requires authentication", "cookies"),
     "Видео доступно только после входа в аккаунт, скачать его не получится."),
    (("Unable to connect to proxy", "ProxyError", "Tunnel connection failed"),
     "Не удалось подключиться к прокси."),
    (("timed out", "Connection refused", "Name or service not known", "Network is unreachable"),
     "Сайт не отвечает, попробуй позже."),
]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("video-bot")

download_slots = asyncio.Semaphore(MAX_PARALLEL)


class DownloadError(Exception):
    pass


@dataclass
class Video:
    path: Path
    title: str
    width: int | None
    height: int | None
    duration: int | None


def format_selector(height: int) -> str:
    # Сначала mp4/h264+m4a (Telegram проигрывает их inline), потом что угодно.
    h = f"[height<={height}]"
    return (
        f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/"
        f"b{h}[ext=mp4]/"
        f"bv*{h}+ba/"
        f"b{h}/"
        f"wv*+ba/w"
    )


def explain_ytdlp_error(stderr: str) -> str:
    lines = stderr.strip().splitlines()
    errors = [l for l in lines if "ERROR" in l] or lines[-3:]
    text = "\n".join(errors)
    for needles, message in YTDLP_ERRORS:
        if any(n in text for n in needles):
            return message
    # Неизвестная ошибка: показываем первую строку без служебного хвоста yt-dlp.
    detail = errors[0] if errors else ""
    detail = detail.removeprefix("ERROR: ").split("; please report this issue")[0]
    return "Не получилось скачать видео." + (f"\n{detail[:500]}" if detail else "")


def normalize_url(raw: str) -> str | None:
    """Добавляет https:// при необходимости; None — если это не http(s)-ссылка."""
    raw = raw.strip().rstrip(".,;:!?)»\"'")
    if not SCHEME_RE.match(raw):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not host:
        return None
    if "." not in host and host != "localhost":
        return None
    return raw


def extract_urls(message: Message) -> tuple[list[str], list[str]]:
    """Возвращает (корректные ссылки, ссылки, которые не удалось разобрать)."""
    types = [MessageEntity.URL, MessageEntity.TEXT_LINK]
    entities = message.parse_entities(types) if message.text else message.parse_caption_entities(types)
    raw = [e.url if e.type == MessageEntity.TEXT_LINK else text for e, text in entities.items()]
    raw += URL_RE.findall(message.text or message.caption or "")

    good, bad = [], []
    for item in raw:
        url = normalize_url(item)
        if url is None:
            if item not in bad:
                bad.append(item)
        elif url not in good:
            good.append(url)
    return good, bad


async def reply(message: Message, text: str) -> Message:
    """Ответ именно на сообщение пользователя (с цитатой и в личке тоже)."""
    return await message.reply_text(text, do_quote=True, disable_web_page_preview=True)


def _int(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


async def download(url: str, height: int, workdir: Path) -> Video:
    cmd = [
        YTDLP_CMD,
        "--no-playlist",
        "--no-progress",
        "--no-simulate",
        "-f", format_selector(height),
        "-S", f"res:{height}",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "-o", str(workdir / "%(id)s.%(ext)s"),
        "--print", "after_move:%(filepath)s\t%(width)s\t%(height)s\t%(duration)s\t%(title)s",
    ]
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]
    cmd.append(url)
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DownloadError("Скачивание заняло слишком много времени.")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        log.warning("yt-dlp failed (%s) for %s:\n%s", proc.returncode, url,
                    "\n".join(err.strip().splitlines()[-10:]))
        raise DownloadError(explain_ytdlp_error(err))

    lines = [l for l in stdout.decode(errors="replace").splitlines() if l.strip()]
    if not lines:
        raise DownloadError("yt-dlp не вернул файл.")
    path, width, vheight, duration, title = (lines[-1].split("\t", 4) + [""] * 5)[:5]
    file = Path(path)
    if not file.exists():
        raise DownloadError("Скачанный файл не найден.")
    video = Video(file, title, _int(width), _int(vheight), _int(duration))
    if None in (video.width, video.height, video.duration):
        await probe(video)
    return video


async def probe(video: Video) -> None:
    """Дозаполняет размеры/длительность через ffprobe, если yt-dlp их не дал."""
    if shutil.which("ffprobe") is None:
        return
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "default=noprint_wrappers=1", str(video.path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    info = dict(
        line.split("=", 1) for line in stdout.decode().splitlines() if "=" in line
    )
    video.width = video.width or _int(info.get("width", ""))
    video.height = video.height or _int(info.get("height", ""))
    video.duration = video.duration or _int(info.get("duration", ""))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(
        update.effective_message,
        f"Пришли ссылку на видео — я скачаю его и пришлю обратно "
        f"(качество не выше {MAX_HEIGHT}p)."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if ALLOWED_USERS and (user is None or user.id not in ALLOWED_USERS):
        log.info("Rejected user %s", user.id if user else None)
        return

    urls, bad = extract_urls(message)
    if bad:
        await reply(
            message,
            "❌ Не удалось распознать ссылку: " + ", ".join(bad)[:300]
            + "\nНужна ссылка вида https://сайт/видео.",
        )
    if not urls:
        if not bad:
            await reply(message, "❌ Не нашёл в сообщении ссылку. Пришли ссылку на видео.")
        return

    for url in urls:
        await process_url(update, context, url)


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Стикеры, фото без подписи и т.п. — подсказываем, что нужна ссылка."""
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await reply(update.effective_message, "❌ Я понимаю только ссылки. Пришли ссылку на видео.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await reply(update.effective_message, "❌ Что-то пошло не так, попробуй ещё раз.")
        except TelegramError:
            log.exception("Failed to report error to user")


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    message = update.effective_message
    status = await reply(message, f"⏳ Скачиваю {url}")
    workdir = Path(tempfile.mkdtemp(prefix="tgvideo-"))
    limit = MAX_FILE_MB * 1024 * 1024
    try:
        async with download_slots:
            video = None
            for height in FALLBACK_HEIGHTS:
                await context.bot.send_chat_action(message.chat_id, ChatAction.RECORD_VIDEO)
                video = await download(url, height, workdir)
                size = video.path.stat().st_size
                log.info("Downloaded %s (%dp max): %.1f MB", url, height, size / 2**20)
                if size <= limit:
                    break
                video.path.unlink(missing_ok=True)
                video = None
                await status.edit_text(
                    f"⏳ В {height}p файл больше {MAX_FILE_MB} МБ, пробую качество пониже…"
                )
            if video is None:
                raise DownloadError(
                    f"Видео слишком большое: даже в низком качестве больше {MAX_FILE_MB} МБ."
                )

            await status.edit_text("📤 Отправляю…")
            await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_VIDEO)
            with video.path.open("rb") as f:
                await message.reply_video(
                    video=f,
                    caption=video.title[:1024] or None,
                    width=video.width,
                    height=video.height,
                    duration=video.duration,
                    supports_streaming=True,
                    read_timeout=600,
                    write_timeout=600,
                )
        await delete_quietly(status)
    except DownloadError as e:
        await fail(message, status, f"❌ {e}\n{url}")
    except TelegramError as e:
        log.exception("Telegram error while processing %s", url)
        await fail(message, status, f"❌ Не удалось отправить видео: {e.message}\n{url}")
    except Exception:
        log.exception("Failed to process %s", url)
        await fail(message, status, f"❌ Что-то пошло не так при обработке ссылки:\n{url}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


async def fail(message: Message, status: Message, text: str) -> None:
    """Убирает статус и отвечает об ошибке на исходное сообщение пользователя."""
    await delete_quietly(status)
    try:
        await reply(message, text)
    except TelegramError:
        log.exception("Failed to report error to user")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Задай BOT_TOKEN (в окружении или в файле .env).")
    if shutil.which(YTDLP_CMD) is None:
        raise SystemExit(f"Команда {YTDLP_CMD!r} не найдена в PATH.")

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .read_timeout(60)
        .write_timeout(600)
        .media_write_timeout(600)
    )
    if BOT_API_URL:
        builder = (
            builder.base_url(f"{BOT_API_URL.rstrip('/')}/bot")
            .base_file_url(f"{BOT_API_URL.rstrip('/')}/file/bot")
            .local_mode(True)
        )
    app = builder.build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    app.add_handler(MessageHandler(~filters.COMMAND & ~filters.StatusUpdate.ALL, handle_other))
    app.add_error_handler(on_error)
    log.info(
        "Bot started (yt-dlp: %s, proxy: %s, max %dp, limit %d MB)",
        YTDLP_CMD, YTDLP_PROXY or "none", MAX_HEIGHT, MAX_FILE_MB,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
