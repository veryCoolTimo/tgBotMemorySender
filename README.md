# MemoryBase Telegram Bot

Бот для добавления записей в базу знаний через Telegram.

## Архитектура

На сервере 2 отдельных репозитория:

```
/root/
├── tg-bot/           ← этот репо (код бота)
│   └── git: tgBotMemorySender.git
│
└── memoryBase/       ← база знаний (куда бот пишет)
    └── git: memoryBase.git
```

Бот читает `REPO_PATH` из `.env` и пушит изменения туда.

## Как работает

1. Отправляешь текст или голосовое сообщение
2. Бот транскрибирует (если голосовое) через Whisper
3. Анализирует через Claude (OpenRouter) куда сохранить
4. Показывает план и спрашивает подтверждение
5. После "Да" — сохраняет в файлы и пушит в git (memoryBase)

## Деплой на сервер

### 1. Клонировать репозиторий memoryBase (база знаний)

```bash
cd /root
git clone git@github.com:veryCoolTimo/memoryBase.git
cd memoryBase
```

### 2. Клонировать репозиторий бота

```bash
cd /root
git clone git@github.com:veryCoolTimo/tgBotMemorySender.git tg-bot
```

### 3. Настроить окружение

```bash
cd /root/tg-bot
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
- `REPO_PATH` — путь к репозиторию (по умолчанию /root/memoryBase)

### 5. Настроить git на сервере

```bash
cd /root/memoryBase
git config user.email "bot@memorybase.local"
git config user.name "MemoryBase Bot"
```

### 6. Запустить бота

#### Вариант A: screen (простой)

```bash
screen -S memorybot
cd /root/tg-bot
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
User=root
WorkingDirectory=/root/tg-bot
Environment=PATH=/root/tg-bot/venv/bin
ExecStart=/root/tg-bot/venv/bin/python bot.py
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

- Отправь текст → бот предложит куда сохранить
- Отправь голосовое → бот транскрибирует и предложит куда сохранить
- Нажми "Да" → сохранено и запушено в git
- Нажми "Нет" → отменено

## Логи

```bash
# systemd
journalctl -u memorybot -f

# screen
screen -r memorybot
```
