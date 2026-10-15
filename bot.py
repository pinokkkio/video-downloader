"""Telegram-бот: присылаешь ссылку — получаешь видео (не выше 720p).

Скачивание идёт через yt-dlp. С Telegram бот работает напрямую по MTProto
(Telethon), поэтому может отправлять файлы до 2 ГБ, а не 50 МБ, как через
Bot API.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events, helpers
from telethon.errors import RPCError
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.custom import Message
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputFileBig,
    MessageEntityTextUrl,
    MessageEntityUrl,
)

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID") or 0)
API_HASH = os.environ.get("API_HASH", "")
# Файл сессии Telethon (без расширения). В Docker — /data/bot (см. Dockerfile).
SESSION = os.environ.get("SESSION", "data/bot")
YTDLP_CMD = os.environ.get("YTDLP_CMD", "yt-dlp")
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "720"))
# Боты по MTProto могут отправлять файлы до 2000 МБ.
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "2000"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "1800"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
# Сколько частей файла отправлять в Telegram одновременно.
UPLOAD_WORKERS = int(os.environ.get("UPLOAD_WORKERS", "8"))
UPLOAD_PART_SIZE = 512 * 1024  # максимальный размер части в MTProto
BIG_FILE_SIZE = 10 * 1024 * 1024  # файлы больше этого грузятся через SaveBigFilePart
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
    (("timed out", "Connection refused", "Name or service not known", "Network is unreachable"),
     "Сайт не отвечает, попробуй позже."),
]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("video-bot")

download_slots = asyncio.Semaphore(MAX_PARALLEL)
START_RE = re.compile(r"^/(start|help)(@\w+)?$", re.I)


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
    raw = []
    for entity, text in message.get_entities_text():
        if isinstance(entity, MessageEntityTextUrl):
            raw.append(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            raw.append(text)
    raw += URL_RE.findall(message.message or "")

    good, bad = [], []
    for item in raw:
        url = normalize_url(item)
        if url is None:
            if item not in bad:
                bad.append(item)
        elif url not in good:
            good.append(url)
    return good, bad


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


async def make_thumb(video: Video) -> Path | None:
    """Кадр для превью: без него Telegram показывает видео с чёрной заглушкой."""
    thumb = video.path.with_suffix(".jpg")
    at = min(1.0, (video.duration or 0) / 2)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-ss", str(at), "-i", str(video.path),
        "-frames:v", "1", "-vf", "scale='min(320,iw)':-2", str(thumb),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return thumb if proc.returncode == 0 and thumb.exists() else None


async def upload_parallel(
    client: TelegramClient, path: Path, progress: Callable[[int, int], Awaitable[None]]
):
    """Загружает файл в Telegram, отправляя несколько частей одновременно.

    Telethon шлёт части строго по очереди и ждёт подтверждения каждой, поэтому
    скорость упирается в задержку до сервера. Параллельная отправка это обходит.
    """
    size = path.stat().st_size
    if size <= BIG_FILE_SIZE or UPLOAD_WORKERS <= 1:
        # Маленькие файлы Telegram принимает только обычным способом (с md5).
        return await client.upload_file(str(path), progress_callback=progress)

    parts = (size + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE
    file_id = helpers.generate_random_long()
    next_part = 0
    sent = 0

    async def worker() -> None:
        nonlocal next_part, sent
        with path.open("rb") as f:
            while next_part < parts:
                index = next_part
                next_part += 1
                f.seek(index * UPLOAD_PART_SIZE)
                data = f.read(UPLOAD_PART_SIZE)
                for attempt in range(3):
                    # FLOOD_WAIT и обрывы связи Telethon обрабатывает сам;
                    # здесь — на случай, если сервер не принял часть.
                    if await client(SaveBigFilePartRequest(file_id, index, parts, data)):
                        break
                    await asyncio.sleep(1 + attempt)
                else:
                    raise RuntimeError(f"Telegram не принял часть {index} файла")
                sent += len(data)
                await progress(sent, size)

    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(min(UPLOAD_WORKERS, parts)):
                group.create_task(worker())
    except ExceptionGroup as eg:
        # Наружу — исходная ошибка, чтобы её поймали обычные обработчики.
        raise eg.exceptions[0] from None
    return InputFileBig(file_id, parts, path.name)


async def reply(message: Message, text: str) -> Message:
    """Ответ именно на сообщение пользователя (с цитатой)."""
    return await message.reply(text, link_preview=False)


async def delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except RPCError:
        pass


async def edit_quietly(message: Message, text: str) -> None:
    try:
        await message.edit(text, link_preview=False)
    except RPCError:
        pass


async def fail(message: Message, status: Message, text: str) -> None:
    """Убирает статус и отвечает об ошибке на исходное сообщение пользователя."""
    await delete_quietly(status)
    try:
        await reply(message, text)
    except RPCError:
        log.exception("Failed to report error to user")


async def on_message(event: events.NewMessage.Event) -> None:
    message: Message = event.message
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        log.info("Rejected user %s", event.sender_id)
        return
    try:
        await handle_message(event, message)
    except Exception:
        log.exception("Unhandled error")
        try:
            await reply(message, "❌ Что-то пошло не так, попробуй ещё раз.")
        except RPCError:
            log.exception("Failed to report error to user")


async def handle_message(event: events.NewMessage.Event, message: Message) -> None:
    text = message.message or ""
    if START_RE.match(text.strip()):
        await reply(
            message,
            f"Пришли ссылку на видео — я скачаю его и пришлю обратно "
            f"(качество не выше {MAX_HEIGHT}p).",
        )
        return

    if not text:
        # Стикеры, фото без подписи и т.п. — подсказываем только в личке.
        if event.is_private:
            await reply(message, "❌ Я понимаю только ссылки. Пришли ссылку на видео.")
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
        await process_url(event.client, message, url)


async def process_url(client: TelegramClient, message: Message, url: str) -> None:
    status = await reply(message, f"⏳ Скачиваю {url}")
    workdir = Path(tempfile.mkdtemp(prefix="tgvideo-"))
    limit = MAX_FILE_MB * 1024 * 1024
    try:
        async with download_slots:
            video = None
            async with client.action(message.chat_id, "record-video"):
                for height in FALLBACK_HEIGHTS:
                    video = await download(url, height, workdir)
                    size = video.path.stat().st_size
                    log.info("Downloaded %s (%dp max): %.1f MB", url, height, size / 2**20)
                    if size <= limit:
                        break
                    video.path.unlink(missing_ok=True)
                    video = None
                    await edit_quietly(
                        status, f"⏳ В {height}p файл больше {MAX_FILE_MB} МБ, пробую качество пониже…"
                    )
            if video is None:
                raise DownloadError(
                    f"Видео слишком большое: даже в низком качестве больше {MAX_FILE_MB} МБ."
                )

            size_mb = video.path.stat().st_size / 2**20
            await edit_quietly(status, f"📤 Отправляю ({size_mb:.0f} МБ)…")
            started = last_edit = time.monotonic()

            async def progress(sent: int, total: int) -> None:
                nonlocal last_edit
                now = time.monotonic()
                if now - last_edit >= 5:
                    last_edit = now
                    speed = sent / 2**20 / max(now - started, 0.001)
                    await edit_quietly(
                        status,
                        f"📤 Отправляю ({size_mb:.0f} МБ): {sent * 100 // total}%, {speed:.1f} МБ/с",
                    )

            thumb = await make_thumb(video)
            async with client.action(message.chat_id, "video"):
                uploaded = await upload_parallel(client, video.path, progress)
                await client.send_file(
                    message.chat_id,
                    uploaded,
                    mime_type="video/mp4",
                    caption=video.title[:1024] or None,
                    reply_to=message.id,
                    thumb=str(thumb) if thumb else None,
                    supports_streaming=True,
                    attributes=[
                        DocumentAttributeVideo(
                            duration=video.duration or 0,
                            w=video.width or 0,
                            h=video.height or 0,
                            supports_streaming=True,
                        ),
                        DocumentAttributeFilename(video.path.name),
                    ],
                )
            elapsed = time.monotonic() - started
            log.info("Uploaded %s: %.1f MB in %.0f s (%.1f MB/s)",
                     url, size_mb, elapsed, size_mb / max(elapsed, 0.001))
        await delete_quietly(status)
    except DownloadError as e:
        await fail(message, status, f"❌ {e}\n{url}")
    except RPCError as e:
        log.exception("Telegram error while processing %s", url)
        await fail(message, status, f"❌ Не удалось отправить видео: {e.message}\n{url}")
    except Exception:
        log.exception("Failed to process %s", url)
        await fail(message, status, f"❌ Что-то пошло не так при обработке ссылки:\n{url}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def run() -> None:
    Path(SESSION).parent.mkdir(parents=True, exist_ok=True)
    # Обычный транспорт MTProto. Обфусцированный (ConnectionTcpObfuscated)
    # здесь не используется: в Telethon он шифрует весь поток на чистом
    # Python (~0,3 МБ/с), и отправка видео становится очень медленной.
    client = TelegramClient(SESSION, API_ID, API_HASH, connection_retries=3)
    client.add_event_handler(on_message, events.NewMessage(incoming=True))

    log.info(
        "Starting bot (yt-dlp: %s, max %dp, limit %d MB)",
        YTDLP_CMD, MAX_HEIGHT, MAX_FILE_MB,
    )
    try:
        await client.start(bot_token=BOT_TOKEN)
    except (OSError, ConnectionError) as e:
        # Без подробного traceback: контейнер перезапустится и попробует снова.
        log.error("Не удалось подключиться к Telegram: %s. Проверь сеть.", e)
        raise SystemExit(1)
    except RPCError as e:
        log.error("Telegram отклонил вход: %s. Проверь BOT_TOKEN, API_ID и API_HASH.", e)
        raise SystemExit(1)

    me = await client.get_me()
    log.info("Bot @%s is running", me.username)
    await client.run_until_disconnected()


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Задай BOT_TOKEN (в окружении или в файле .env).")
    if not API_ID or not API_HASH:
        raise SystemExit("Задай API_ID и API_HASH (их выдают на https://my.telegram.org).")
    if shutil.which(YTDLP_CMD) is None:
        raise SystemExit(f"Команда {YTDLP_CMD!r} не найдена в PATH.")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
