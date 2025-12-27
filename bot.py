import os
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import openai
import httpx
import subprocess

load_dotenv()

# Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
REPO_PATH = os.getenv("REPO_PATH", "/root/memoryBase")

# OpenAI client for Whisper
whisper_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Pending actions storage (in-memory, per session)
pending_actions = {}


async def analyze_with_claude(text: str) -> dict:
    """Analyze text with Claude via OpenRouter and decide where to put it."""

    today = datetime.now().strftime("%Y-%m-%d")
    time_now = datetime.now().strftime("%H:%M")

    prompt = f"""Ты помощник для организации базы знаний. Проанализируй текст и определи куда его сохранить.

Доступные папки:
- daily/{datetime.now().strftime("%Y/%m")}/{datetime.now().strftime("%d")}.md — ежедневные записи
- projects/{{project_name}}/log.md — логи проектов
- notes/{{название}}.md — отдельные заметки
- ideas/{{название}}.md — идеи
- people/{{имя}}.md — заметки о людях
- books-manga/{{название}}.md — про книги и мангу

Правила:
1. Если это про день/что делал — в daily
2. Если упоминается проект — добавить и в daily, и в projects/{{project}}/log.md
3. Если про человека — добавить и в основную категорию, и в people/
4. Одна запись может идти в несколько мест (cross-linking)

Текст для анализа:
"{text}"

Ответь в формате JSON:
{{
    "actions": [
        {{
            "file": "путь/к/файлу.md",
            "action": "append" или "create",
            "content": "что добавить (с форматированием markdown)",
            "description": "краткое описание для пользователя"
        }}
    ],
    "summary": "краткое описание что будет сделано (1-2 предложения на русском)"
}}

Дата сегодня: {today}
Время: {time_now}
Добавляй временные метки в daily записи."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "anthropic/claude-sonnet-4",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=60.0,
        )

        result = response.json()
        content = result["choices"][0]["message"]["content"]

        # Parse JSON from response
        import json
        # Find JSON in response
        start = content.find("{")
        end = content.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(content[start:end])

        return {"actions": [], "summary": "Не удалось проанализировать"}


async def transcribe_voice(file_path: str) -> str:
    """Transcribe voice message using OpenAI Whisper."""
    with open(file_path, "rb") as audio_file:
        transcript = whisper_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru"
        )
    return transcript.text


def apply_actions(actions: list) -> bool:
    """Apply file actions to the repository."""
    repo = Path(REPO_PATH)

    for action in actions:
        file_path = repo / action["file"]
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if action["action"] == "append":
            with open(file_path, "a", encoding="utf-8") as f:
                f.write("\n" + action["content"] + "\n")
        else:  # create
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(action["content"])

    return True


def git_commit_and_push(message: str) -> bool:
    """Commit and push changes."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=REPO_PATH, check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=REPO_PATH,
            check=True
        )
        subprocess.run(["git", "push"], cwd=REPO_PATH, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return

    await update.message.reply_text(
        "Привет! Отправь мне текст или голосовое сообщение, "
        "и я помогу сохранить это в базу знаний."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    text = update.message.text
    await process_input(update, context, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    await update.message.reply_text("Транскрибирую голосовое...")

    # Download voice file
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)

        # Transcribe
        text = await transcribe_voice(tmp.name)
        os.unlink(tmp.name)

    await update.message.reply_text(f"Транскрипт:\n\n{text}")
    await process_input(update, context, text)


async def process_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Process input text and ask for confirmation."""
    await update.message.reply_text("Анализирую...")

    # Analyze with Claude
    analysis = await analyze_with_claude(text)

    if not analysis.get("actions"):
        await update.message.reply_text("Не удалось определить куда сохранить.")
        return

    # Store pending action
    user_id = update.effective_user.id
    pending_actions[user_id] = {
        "actions": analysis["actions"],
        "original_text": text,
    }

    # Format response
    summary = analysis.get("summary", "")
    details = "\n".join([
        f"• {a['description']}"
        for a in analysis["actions"]
    ])

    keyboard = [
        [
            InlineKeyboardButton("Да", callback_data="confirm"),
            InlineKeyboardButton("Нет", callback_data="cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"{summary}\n\n{details}\n\nСохранить?",
        reply_markup=reply_markup
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if user_id != ALLOWED_USER_ID:
        return

    if query.data == "confirm":
        pending = pending_actions.get(user_id)
        if not pending:
            await query.edit_message_text("Нет ожидающих действий.")
            return

        # Apply actions
        apply_actions(pending["actions"])

        # Commit and push
        today = datetime.now().strftime("%Y-%m-%d")
        git_commit_and_push(f"{today}: добавлено через Telegram")

        await query.edit_message_text("Сохранено и запушено в git!")
        del pending_actions[user_id]

    elif query.data == "cancel":
        if user_id in pending_actions:
            del pending_actions[user_id]
        await query.edit_message_text("Отменено.")


def main():
    """Start the bot."""
    if not all([TELEGRAM_TOKEN, OPENROUTER_API_KEY, OPENAI_API_KEY, ALLOWED_USER_ID]):
        print("Error: Missing environment variables")
        print("Required: TELEGRAM_TOKEN, OPENROUTER_API_KEY, OPENAI_API_KEY, ALLOWED_USER_ID")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(CallbackQueryHandler(handle_callback))

    print(f"Bot started. Repo path: {REPO_PATH}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
