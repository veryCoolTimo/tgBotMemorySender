import os
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from aiohttp import web

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
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
API_PORT = int(os.getenv("API_PORT", "8585"))
API_SECRET = os.getenv("API_SECRET", "")  # Optional secret for API auth

# Load prompt template
SCRIPT_DIR = Path(__file__).parent
PROMPT_FILE = SCRIPT_DIR / "prompt.txt"

def load_prompt_template() -> str:
    """Load prompt template from file."""
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8")
    return ""

# OpenAI client for Whisper
whisper_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Pending actions storage (in-memory, per session)
pending_actions = {}

# Pending insights from MCP
pending_insights = {}

# User state for edit mode
user_states = {}

# Telegram bot instance for API use
bot_instance: Bot = None


# ============== API HANDLERS ==============

async def handle_insight_api(request):
    """Handle POST /api/insight from MCP."""
    global bot_instance

    # Check secret if configured
    if API_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {API_SECRET}":
            return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Validate required fields
    required = ["type", "project", "summary"]
    for field in required:
        if field not in data:
            return web.json_response({"error": f"Missing field: {field}"}, status=400)

    # Create insight record
    insight_id = datetime.now().strftime("%Y%m%d%H%M%S")

    # Format text for Claude analysis (same as regular messages)
    insight_text = f"""[{data.get('type', 'info').upper()}] {data.get('project', 'unknown')}

{data.get('summary', '')}

{data.get('description', '')}"""

    # Send "analyzing" message first
    try:
        status_msg = await bot_instance.send_message(
            chat_id=ALLOWED_USER_ID,
            text="Анализирую insight от Claude..."
        )
    except Exception as e:
        return web.json_response({"error": f"Telegram error: {str(e)}"}, status=500)

    # Analyze with Claude (same as regular messages)
    analysis = await analyze_with_claude(insight_text)

    if not analysis.get("actions"):
        await bot_instance.edit_message_text(
            chat_id=ALLOWED_USER_ID,
            message_id=status_msg.message_id,
            text="Не удалось проанализировать insight."
        )
        return web.json_response({"error": "Analysis failed"}, status=500)

    # Store pending insight with pre-analyzed actions
    pending_insights[insight_id] = {
        "id": insight_id,
        "timestamp": datetime.now().isoformat(),
        "type": data.get("type"),
        "project": data.get("project"),
        "summary": data.get("summary"),
        "description": data.get("description", ""),
        "files_changed": data.get("files_changed", []),
        "original_text": insight_text,
        "actions": analysis["actions"],
    }

    # Format message same as regular flow
    keyboard = [
        [
            InlineKeyboardButton("Да", callback_data=f"insight_confirm:{insight_id}"),
            InlineKeyboardButton("Изменить", callback_data=f"insight_edit:{insight_id}"),
            InlineKeyboardButton("Нет", callback_data=f"insight_cancel:{insight_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await bot_instance.edit_message_text(
        chat_id=ALLOWED_USER_ID,
        message_id=status_msg.message_id,
        text=format_analysis_message(analysis),
        reply_markup=reply_markup
    )

    return web.json_response({
        "success": True,
        "insight_id": insight_id,
        "message": "Insight sent to Telegram"
    })


async def handle_health(request):
    """Health check endpoint."""
    return web.json_response({"status": "ok"})


# ============== TELEGRAM HANDLERS ==============

async def analyze_with_claude(text: str, edit_instructions: str = None) -> dict:
    """Analyze text with Claude via OpenRouter and decide where to put it."""

    today = datetime.now().strftime("%Y-%m-%d")
    time_now = datetime.now().strftime("%H:%M")

    edit_part = ""
    if edit_instructions:
        edit_part = f"""ВАЖНО: Пользователь попросил изменить предыдущий анализ:
"{edit_instructions}"

Учти эти изменения при формировании ответа."""

    # Load prompt from file
    prompt_template = load_prompt_template()

    # Replace placeholders
    prompt = prompt_template.replace("{TEXT}", text)
    prompt = prompt.replace("{DATE}", today)
    prompt = prompt.replace("{TIME}", time_now)
    prompt = prompt.replace("{YYYY}", datetime.now().strftime("%Y"))
    prompt = prompt.replace("{MM}", datetime.now().strftime("%m"))
    prompt = prompt.replace("{DD}", datetime.now().strftime("%d"))
    prompt = prompt.replace("{EDIT_INSTRUCTIONS}", edit_part)

    # Use faster model for edits, main model for initial analysis
    model = "google/gemini-2.5-flash-preview" if edit_instructions else "anthropic/claude-sonnet-4"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
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


def git_pull() -> bool:
    """Pull latest changes before writing files."""
    try:
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=REPO_PATH,
            check=True,
            capture_output=True
        )
        return True
    except subprocess.CalledProcessError:
        return False


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
    files_list = "\n".join([f"  - {a['file']}" for a in actions])

    # Details
    details = "\n".join([f"- {a['description']}" for a in actions])

    return f"""{summary}

Файлы для записи:
{files_list}

Что будет добавлено:
{details}

Сохранить?"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return

    await update.message.reply_text(
        "Привет! Отправь мне текст или голосовое сообщение, "
        "и я помогу сохранить это в базу знаний.\n\n"
        "Также я принимаю insights от Claude через API."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    user_id = update.effective_user.id
    text = update.message.text

    # Check if user is in edit mode for insight
    state = user_states.get(user_id)
    if state and state.startswith("insight_editing:"):
        insight_id = state.split(":")[1]
        await handle_insight_edit_input(update, context, insight_id, text)
        return

    # Check if user is in edit mode
    if state == "editing":
        await handle_edit_input(update, context, text)
        return

    await process_input(update, context, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    user_id = update.effective_user.id

    # Check if user is in edit mode for insight
    state = user_states.get(user_id)
    if state and state.startswith("insight_editing:"):
        insight_id = state.split(":")[1]
        status_msg = await update.message.reply_text("Транскрибирую...")

        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            text = await transcribe_voice(tmp.name)
            os.unlink(tmp.name)

        await status_msg.edit_text(f"Транскрипт: {text}")
        await handle_insight_edit_input(update, context, insight_id, text)
        return

    # Check if user is in edit mode
    if state == "editing":
        # Transcribe and use as edit instructions
        status_msg = await update.message.reply_text("Транскрибирую...")

        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            text = await transcribe_voice(tmp.name)
            os.unlink(tmp.name)

        await status_msg.edit_text(f"Транскрипт: {text}")
        await handle_edit_input(update, context, text)
        return

    # Normal voice processing
    status_msg = await update.message.reply_text("Транскрибирую голосовое...")

    # Download voice file
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        text = await transcribe_voice(tmp.name)
        os.unlink(tmp.name)

    # Edit the status message with transcript
    await status_msg.edit_text(f"Транскрипт:\n\n{text}")

    await process_input(update, context, text)


async def handle_insight_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE, insight_id: str, edit_text: str):
    """Handle edit instructions for insight."""
    user_id = update.effective_user.id
    insight = pending_insights.get(insight_id)

    if not insight:
        user_states[user_id] = None
        await update.message.reply_text("Insight не найден.")
        return

    # Clear edit mode
    user_states[user_id] = None

    # Send analyzing message
    status_msg = await update.message.reply_text("Анализирую с учётом изменений...")

    # Re-analyze with edit instructions (same as regular edit flow)
    original_text = insight.get("original_text", "")
    analysis = await analyze_with_claude(original_text, edit_text)

    if not analysis.get("actions"):
        await status_msg.edit_text("Не удалось обработать изменения.")
        return

    # Update insight with new actions
    insight["actions"] = analysis["actions"]

    # Format message same as regular flow
    keyboard = [
        [
            InlineKeyboardButton("Да", callback_data=f"insight_confirm:{insight_id}"),
            InlineKeyboardButton("Изменить", callback_data=f"insight_edit:{insight_id}"),
            InlineKeyboardButton("Нет", callback_data=f"insight_cancel:{insight_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await status_msg.edit_text(
        format_analysis_message(analysis),
        reply_markup=reply_markup
    )


async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_text: str):
    """Handle edit instructions from user."""
    user_id = update.effective_user.id
    pending = pending_actions.get(user_id)

    if not pending:
        user_states[user_id] = None
        await update.message.reply_text("Нет ожидающих действий для редактирования.")
        return

    # Clear edit mode
    user_states[user_id] = None

    # Send analyzing message
    status_msg = await update.message.reply_text("Анализирую с учётом изменений...")

    # Re-analyze with edit instructions
    analysis = await analyze_with_claude(pending["original_text"], edit_text)

    if not analysis.get("actions"):
        await status_msg.edit_text("Не удалось обработать изменения.")
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
            InlineKeyboardButton("Да", callback_data="confirm"),
            InlineKeyboardButton("Изменить", callback_data="edit"),
            InlineKeyboardButton("Нет", callback_data="cancel"),
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
    status_msg = await update.message.reply_text("Анализирую...")

    # Analyze with Claude
    analysis = await analyze_with_claude(text)

    if not analysis.get("actions"):
        await status_msg.edit_text("Не удалось определить куда сохранить.")
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
            InlineKeyboardButton("Да", callback_data="confirm"),
            InlineKeyboardButton("Изменить", callback_data="edit"),
            InlineKeyboardButton("Нет", callback_data="cancel"),
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

    data = query.data

    # Handle insight callbacks
    if data.startswith("insight_"):
        action, insight_id = data.split(":", 1)
        insight = pending_insights.get(insight_id)

        if not insight:
            await query.edit_message_text("Insight не найден или уже обработан.")
            return

        if action == "insight_confirm":
            # Use pre-analyzed actions (already analyzed when insight arrived)
            actions = insight.get("actions", [])

            if actions:
                git_pull()  # Pull before writing files
                apply_actions(actions)
                today = datetime.now().strftime("%Y-%m-%d")
                success = git_commit_and_push(f"{today}: insight from Claude - {insight['summary'][:50]}")

                if success:
                    files = "\n".join([f"  - {a['file']}" for a in actions])
                    await query.edit_message_text(f"Сохранено и запушено!\n\nФайлы:\n{files}")
                else:
                    await query.edit_message_text("Сохранено локально, но не удалось запушить.")
            else:
                await query.edit_message_text("Нет действий для выполнения.")

            del pending_insights[insight_id]

        elif action == "insight_edit":
            user_states[user_id] = f"insight_editing:{insight_id}"
            current_text = query.message.text
            await query.edit_message_text(
                f"{current_text}\n\n---\nЧто изменить? (текст или голосовое)"
            )

        elif action == "insight_cancel":
            del pending_insights[insight_id]
            await query.edit_message_text("Insight отменён.")

        return

    # Handle regular callbacks
    if data == "confirm":
        pending = pending_actions.get(user_id)
        if not pending:
            await query.edit_message_text("Нет ожидающих действий.")
            return

        # Pull, apply actions, commit and push
        git_pull()  # Pull before writing files
        apply_actions(pending["actions"])

        today = datetime.now().strftime("%Y-%m-%d")
        success = git_commit_and_push(f"{today}: добавлено через Telegram")

        if success:
            # Format saved files list
            files = "\n".join([f"  - {a['file']}" for a in pending["actions"]])
            await query.edit_message_text(f"Сохранено и запушено!\n\nФайлы:\n{files}")
        else:
            await query.edit_message_text("Сохранено локально, но не удалось запушить в git.")

        del pending_actions[user_id]

    elif data == "edit":
        # Enter edit mode
        user_states[user_id] = "editing"
        # Keep context visible, append edit prompt
        current_text = query.message.text
        await query.edit_message_text(
            f"{current_text}\n\n---\nЧто изменить? (текст или голосовое)"
        )

    elif data == "cancel":
        if user_id in pending_actions:
            del pending_actions[user_id]
        user_states[user_id] = None
        await query.edit_message_text("Отменено.")


async def run_api_server(app: web.Application):
    """Run the API server."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    print(f"API server running on port {API_PORT}")


async def main():
    """Start both Telegram bot and API server."""
    global bot_instance

    if not all([TELEGRAM_TOKEN, OPENROUTER_API_KEY, OPENAI_API_KEY, ALLOWED_USER_ID]):
        print("Error: Missing environment variables")
        print("Required: TELEGRAM_TOKEN, OPENROUTER_API_KEY, OPENAI_API_KEY, ALLOWED_USER_ID")
        return

    # Create Telegram application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_instance = application.bot

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Create API app
    api_app = web.Application()
    api_app.router.add_post("/api/insight", handle_insight_api)
    api_app.router.add_get("/health", handle_health)

    print(f"Bot started. Repo path: {REPO_PATH}")
    print(f"API endpoint: http://0.0.0.0:{API_PORT}/api/insight")

    # Run both
    async with application:
        await application.start()
        await run_api_server(api_app)
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        # Keep running
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
