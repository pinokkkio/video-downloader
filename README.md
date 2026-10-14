# video-downloader

Telegram-бот: присылаешь ссылку — бот отвечает видеофайлом (не выше 720p).
Скачивает через `yt-dlp-proxy` (yt-dlp с прокси по умолчанию), так что
поддерживаются все сайты, которые умеет yt-dlp.

## Требования

- Python 3.10+
- `yt-dlp-proxy` в `PATH`
- `ffmpeg` (нужен yt-dlp, чтобы склеить видео и звук в mp4)

## Установка

```bash
git clone <repo> ~/video-downloader && cd ~/video-downloader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # и вписать BOT_TOKEN от @BotFather
```

Проверить в терминале:

```bash
.venv/bin/python bot.py
```

## Фоновый запуск

### Linux (systemd)

```bash
mkdir -p ~/.config/systemd/user
cp video-bot.service ~/.config/systemd/user/
# если проект лежит не в ~/video-downloader или yt-dlp-proxy в другом месте —
# поправь WorkingDirectory/ExecStart/PATH в скопированном файле
systemctl --user daemon-reload
systemctl --user enable --now video-bot
loginctl enable-linger "$USER"   # чтобы бот работал без открытой сессии

journalctl --user -u video-bot -f   # логи
```

### Без systemd (Linux/macOS)

```bash
nohup .venv/bin/python bot.py >> bot.log 2>&1 &
```

## Как работает

1. Бот находит в сообщении все ссылки `http(s)://…`.
2. Для каждой запускает `yt-dlp-proxy` с выбором формата не выше 720p
   (предпочитая h264/mp4, который Telegram воспроизводит прямо в чате).
3. Если файл больше лимита Telegram (50 МБ), качает заново в 480p, 360p, 240p.
4. Отправляет видео ответом на сообщение и удаляет временные файлы.

## Настройки (`.env`)

| Переменная         | По умолчанию   | Описание |
|--------------------|----------------|----------|
| `BOT_TOKEN`        | —              | Токен бота (обязательно) |
| `ALLOWED_USERS`    | пусто (все)    | Telegram id через запятую, кому разрешено пользоваться ботом |
| `YTDLP_CMD`        | `yt-dlp-proxy` | Команда для скачивания |
| `MAX_HEIGHT`       | `720`          | Максимальная высота видео |
| `MAX_FILE_MB`      | `50`           | Лимит размера файла |
| `BOT_API_URL`      | пусто          | Локальный [telegram-bot-api](https://github.com/tdlib/telegram-bot-api), например `http://localhost:8081` — тогда можно поднять `MAX_FILE_MB` до 2000 |
| `DOWNLOAD_TIMEOUT` | `600`          | Таймаут скачивания, сек. |
| `MAX_PARALLEL`     | `2`            | Сколько видео качать одновременно |

Свой Telegram id можно узнать у [@userinfobot](https://t.me/userinfobot).
