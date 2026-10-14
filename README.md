# video-downloader

Telegram-бот: присылаешь ссылку — бот отвечает видеофайлом (не выше 720p).
Скачивает через `yt-dlp-proxy` (yt-dlp с прокси по умолчанию), так что
поддерживаются все сайты, которые умеет yt-dlp.

## Запуск в Docker

Нужны Docker и Docker Compose. Всё остальное есть внутри образа:
yt-dlp, ffmpeg, deno (yt-dlp нужен JS-рантайм для YouTube) и
обёртка `yt-dlp-proxy`, которая вызывает `yt-dlp --proxy "$YTDLP_PROXY"`.

```bash
git clone <repo> video-downloader && cd video-downloader
cp .env.example .env    # вписать BOT_TOKEN от @BotFather и YTDLP_PROXY
docker compose up -d --build
```

Контейнер запускается в фоне и сам поднимается после перезагрузки
(`restart: unless-stopped`, если служба Docker включена при старте системы).

```bash
docker compose logs -f                  # логи
docker compose down                     # остановить
docker compose build --no-cache && docker compose up -d   # обновить yt-dlp
```

Если сайт перестал скачиваться, сначала обнови yt-dlp последней командой:
при каждой сборке ставится свежая версия.

### Прокси на этом же компьютере

Внутри контейнера `127.0.0.1` указывает на сам контейнер, а не на твой
компьютер. Поэтому в `YTDLP_PROXY` вместо `127.0.0.1` пиши
`host.docker.internal`, например `socks5://host.docker.internal:1080`.
Прокси должен принимать подключения не только с `127.0.0.1`: укажи в его
настройках адрес `0.0.0.0` или адрес моста Docker.

Другой способ, только для Linux: добавить в `compose.yaml` сервису
`network_mode: host`, убрав `extra_hosts`. Тогда подойдёт и `127.0.0.1`.

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
| `YTDLP_PROXY`      | пусто          | Прокси для yt-dlp (`socks5://…`, `http://…`) |
| `MAX_HEIGHT`       | `720`          | Максимальная высота видео |
| `MAX_FILE_MB`      | `50`           | Лимит размера файла |
| `BOT_API_URL`      | пусто          | Локальный [telegram-bot-api](https://github.com/tdlib/telegram-bot-api), например `http://host.docker.internal:8081` — тогда можно поднять `MAX_FILE_MB` до 2000 |
| `DOWNLOAD_TIMEOUT` | `600`          | Таймаут скачивания, сек. |
| `MAX_PARALLEL`     | `2`            | Сколько видео качать одновременно |

Свой Telegram id можно узнать у [@userinfobot](https://t.me/userinfobot).
