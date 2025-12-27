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
import json

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

# User state for edit mode
user_states = {}


async def analyze_with_claude(text: str, edit_instructions: str = None) -> dict:
    """Analyze text with Claude via OpenRouter and decide where to put it."""

    today = datetime.now().strftime("%Y-%m-%d")
    time_now = datetime.now().strftime("%H:%M")

    edit_part = ""
    if edit_instructions:
        edit_part = f"""
ВАЖНО: Пользователь попросил изменить предыдущий анализ:
"{edit_instructions}"

Учти эти изменения при формировании ответа.
"""

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
{edit_part}
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


def format_analysis_message(analysis: dict) -> str:
    """Format analysis results for display."""
    summary = analysis.get("summary", "")
    actions = analysis.get("actions", [])

    # Group files
    files_list = "\n".join([f"  📄 {a['file']}" for a in actions])

    # Details
    details = "\n".join([f"• {a['description']}" for a in actions])

    return f"""📝 {summary}

📁 Файлы для записи:
{files_list}

📋 Что будет добавлено:
{details}

Сохранить?"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return

    await update.message.reply_text(
        "👋 Привет! Отправь мне текст или голосовое сообщение, "
        "и я помогу сохранить это в базу знаний."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    user_id = update.effective_user.id
    text = update.message.text

    # Check if user is in edit mode
    if user_states.get(user_id) == "editing":
        await handle_edit_input(update, context, text)
        return

    await process_input(update, context, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    user_id = update.effective_user.id

    # Check if user is in edit mode
    if user_states.get(user_id) == "editing":
        # Transcribe and use as edit instructions
        status_msg = await update.message.reply_text("🎤 Транскрибирую...")

        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            text = await transcribe_voice(tmp.name)
            os.unlink(tmp.name)

        await status_msg.edit_text(f"🎤 Транскрипт: {text}")
        await handle_edit_input(update, context, text)
        return

    # Normal voice processing
    status_msg = await update.message.reply_text("🎤 Транскрибирую голосовое...")

    # Download voice file
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        text = await transcribe_voice(tmp.name)
        os.unlink(tmp.name)

    # Edit the status message with transcript
    await status_msg.edit_text(f"🎤 Транскрипт:\n\n{text}")

    await process_input(update, context, text)


async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_text: str):
    """Handle edit instructions from user."""
    user_id = update.effective_user.id
    pending = pending_actions.get(user_id)

    if not pending:
        user_states[user_id] = None
        await update.message.reply_text("❌ Нет ожидающих действий для редактирования.")
        return

    # Clear edit mode
    user_states[user_id] = None

    # Send analyzing message
    status_msg = await update.message.reply_text("🔄 Анализирую с учётом изменений...")

    # Re-analyze with edit instructions
    analysis = await analyze_with_claude(pending["original_text"], edit_text)

    if not analysis.get("actions"):
        await status_msg.edit_text("❌ Не удалось обработать изменения.")
        return

    # Update pending actions
    pending_actions[user_id] = {
        "actions": analysis["actions"],
        "original_text": pending["original_text"],
        "analysis_message_id": status_msg.message_id,
    }

    # Format and show updated analysis
    keyboard = [
        [
            InlineKeyboardButton("✅ Да", callback_data="confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="edit"),
            InlineKeyboardButton("❌ Нет", callback_data="cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await status_msg.edit_text(
        format_analysis_message(analysis),
        reply_markup=reply_markup
    )


async def process_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Process input text and ask for confirmation."""
    # Send analyzing message
    status_msg = await update.message.reply_text("🔄 Анализирую...")

    # Analyze with Claude
    analysis = await analyze_with_claude(text)

    if not analysis.get("actions"):
        await status_msg.edit_text("❌ Не удалось определить куда сохранить.")
        return

    # Store pending action
    user_id = update.effective_user.id
    pending_actions[user_id] = {
        "actions": analysis["actions"],
        "original_text": text,
        "analysis_message_id": status_msg.message_id,
    }

    # Format response with buttons
    keyboard = [
        [
            InlineKeyboardButton("✅ Да", callback_data="confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="edit"),
            InlineKeyboardButton("❌ Нет", callback_data="cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Edit the status message with analysis
    await status_msg.edit_text(
        format_analysis_message(analysis),
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
            await query.edit_message_text("❌ Нет ожидающих действий.")
            return

        # Apply actions
        apply_actions(pending["actions"])

        # Commit and push
        today = datetime.now().strftime("%Y-%m-%d")
        success = git_commit_and_push(f"{today}: добавлено через Telegram")

        if success:
            # Format saved files list
            files = "\n".join([f"  ✅ {a['file']}" for a in pending["actions"]])
            await query.edit_message_text(f"✅ Сохранено и запушено!\n\n📁 Файлы:\n{files}")
        else:
            await query.edit_message_text("⚠️ Сохранено локально, но не удалось запушить в git.")

        del pending_actions[user_id]

    elif query.data == "edit":
        # Enter edit mode
        user_states[user_id] = "editing"
        await query.edit_message_text(
            "✏️ Опишите что нужно изменить (текстом или голосовым сообщением):"
        )

    elif query.data == "cancel":
        if user_id in pending_actions:
            del pending_actions[user_id]
        user_states[user_id] = None
        await query.edit_message_text("❌ Отменено.")


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
