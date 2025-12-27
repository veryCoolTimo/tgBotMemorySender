# MemoryBase Telegram Bot

Бот для добавления записей в базу знаний через Telegram.

## Возможности

- Отправляй текст или голосовые сообщения
- Бот транскрибирует голосовые через Whisper
- Анализирует через Claude (OpenRouter) куда сохранить
- Показывает план с файлами и спрашивает подтверждение
- Кнопка "Изменить" — можно скорректировать голосом или текстом
- После подтверждения — коммитит и пушит в git
- **API для MCP**: принимает insights от Claude Code через HTTP

## Структура

```
tg-bot/
├── bot.py           # Основной код
├── prompt.txt       # Промт для ИИ (редактируй под себя)
├── .env             # Токены и настройки
├── .env.example     # Пример .env
└── requirements.txt # Зависимости
```

## Кастомизация промта

Редактируй `prompt.txt` под свои правила. Доступные плейсхолдеры:

| Плейсхолдер | Описание |
|-------------|----------|
| `{TEXT}` | Текст пользователя |
| `{DATE}` | Дата (2025-12-27) |
| `{TIME}` | Время (14:30) |
| `{YYYY}` | Год |
| `{MM}` | Месяц |
| `{DD}` | День |
| `{EDIT_INSTRUCTIONS}` | Инструкции для редактирования |

## Архитектура

На сервере 2 отдельных репозитория:

```
~/
├── tg-bot/           <- этот репо (код бота)
│
└── memoryBase/       <- база знаний (куда бот пишет)
```

Бот читает `REPO_PATH` из `.env` и пушит изменения туда.

## Деплой на сервер

### 1. Клонировать репозиторий базы знаний

```bash
cd ~
git clone git@github.com:YOUR_USERNAME/memoryBase.git
cd memoryBase
git config user.email "bot@memorybase.local"
git config user.name "MemoryBase Bot"
```

### 2. Клонировать репозиторий бота

```bash
cd ~
git clone git@github.com:YOUR_USERNAME/tgBotMemorySender.git tg-bot
```

### 3. Настроить окружение

```bash
cd ~/tg-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Создать .env файл

```bash
cp .env.example .env
nano .env
```

Заполнить:
- `TELEGRAM_TOKEN` — получить у @BotFather
- `ALLOWED_USER_ID` — твой Telegram ID (узнать у @userinfobot)
- `OPENROUTER_API_KEY` — https://openrouter.ai/keys
- `OPENAI_API_KEY` — https://platform.openai.com/api-keys
- `REPO_PATH` — полный путь к репозиторию базы знаний
- `API_PORT` — порт для API (по умолчанию 8585)
- `API_SECRET` — секретный ключ для API (опционально)

### 5. Запустить бота

#### Вариант A: screen (простой)

```bash
screen -S memorybot
cd ~/tg-bot
source venv/bin/activate
python bot.py
# Ctrl+A, D для выхода из screen
```

#### Вариант B: systemd (надёжный)

```bash
sudo nano /etc/systemd/system/memorybot.service
```

```ini
[Unit]
Description=MemoryBase Telegram Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/tg-bot
Environment=PATH=/home/YOUR_USER/tg-bot/venv/bin
ExecStart=/home/YOUR_USER/tg-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable memorybot
sudo systemctl start memorybot
sudo systemctl status memorybot
```

## Использование

1. Отправь текст или голосовое
2. Бот покажет куда сохранит:
   ```
   Запись будет добавлена в ежедневник и лог проекта.

   Файлы для записи:
     - daily/2025/12/27.md
     - projects/MyProject/log.md

   Что будет добавлено:
   - Запись о работе над проектом
   - Лог в проект

   Сохранить?
   [Да] [Изменить] [Нет]
   ```
3. `Да` — сохранить и запушить
4. `Изменить` — скорректировать голосом или текстом
5. `Нет` — отменить

## Логи

```bash
# systemd
journalctl -u memorybot -f

# screen
screen -r memorybot
```

## API для MCP

Бот также запускает HTTP API сервер для приёма insights от MCP.

### Endpoints

- `POST /api/insight` — принять insight
- `GET /health` — проверка работы

### Формат запроса

```json
{
  "type": "feature|bugfix|plan|idea|decision|learning",
  "project": "project-name",
  "summary": "Краткое описание",
  "description": "Подробное описание",
  "files_changed": ["file1.py", "file2.py"]
}
```

### Заголовки

- `Authorization: Bearer YOUR_API_SECRET` (если настроен API_SECRET)

### Как это работает

1. Claude Code вызывает `log_insight` через MCP
2. MCP отправляет HTTP запрос на сервер
3. Бот отправляет уведомление в Telegram с кнопками [Да] [Изменить] [Нет]
4. При подтверждении — анализирует и сохраняет в memoryBase

## Лицензия

MIT
