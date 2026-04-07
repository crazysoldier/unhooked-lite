"""Unhooked Lite — complete Telegram bot in one file.

Replaces the original's 40+ files with a single entry point that handles:
- Onboarding (/start)
- Streak tracking (/status, /reset, /undo)
- Crisis toolkit (/sos — grounding, breathing, urge surfing, call, distraction)
- AI coaching (free-text messages via OpenAI or Anthropic)
- Journaling (/journal)
- Savings tracking (/savings)
- Quick mood check-in (/check)
- Proactive scheduled messages (morning, nudge, evening)
- Chat whitelist security

"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    JobQueue,
    MessageHandler,
    TypeHandler,
    filters,
)

from coach import AIClient, History, detect_intent
from models import Store, UserState

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("unhooked")

# ── Config ────────────────────────────────────────────────────────────────

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Vienna")
try:
    ZoneInfo(TIMEZONE)
except Exception:
    log.error("Invalid TIMEZONE: %s. Falling back to Europe/Vienna.", TIMEZONE)
    TIMEZONE = "Europe/Vienna"
DATA_DIR = os.getenv("DATA_DIR", "./data")

_raw_ids = os.getenv("ALLOWED_TELEGRAM_CHAT_IDS", "")
ALLOWED_IDS: set[int] = set()
for x in _raw_ids.split(","):
    if x.strip():
        try:
            ALLOWED_IDS.add(int(x.strip()))
        except ValueError:
            pass

# ── Conversation states ───────────────────────────────────────────────────

START, REGISTER, CONTACT, JOURNAL, EMERGENCY_CONTACT = range(5)


# ── Helpers ───────────────────────────────────────────────────────────────


def _get_tz() -> ZoneInfo:
    """Get the bot's timezone."""
    return ZoneInfo(TIMEZONE)


def _now() -> datetime:
    """Get the current time in the bot's timezone."""
    return datetime.now(_get_tz())


def _today() -> date:
    """Get today's date in the bot's timezone."""
    return _now().date()


def _log_command(user_id: int, cmd: str) -> None:
    """Log a command execution."""
    log.info("[user=%s] /%s", user_id, cmd)


async def _check_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is in the whitelist. If not, send a denial message."""
    if not ALLOWED_IDS:
        # No whitelist configured, allow all
        return True

    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ALLOWED_IDS:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return False
    return True


async def _send_markdown(msg: Message, text: str) -> None:
    """Send markdown-formatted text."""
    try:
        await msg.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        log.warning("Markdown rendering failed: %s. Sending as plain text.", e)
        await msg.reply_text(text)


# ── Onboarding ───────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start command."""
    if not await _check_whitelist(update, context):
        return -1

    user_id = update.effective_user.id
    _log_command(user_id, "start")

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if state and state.name:
        msg = (
            f"Welcome back, {state.name}! 👋\n\n"
            "What would you like to do?\n\n"
            "• /status - Check your streak\n"
            "• /sos - Crisis toolkit\n"
            "• /journal - Write in your journal\n"
            "• /check - Quick mood check-in\n"
            "• /savings - View savings progress\n"
            "• /help - Show all commands"
        )
        await update.message.reply_text(msg)
        return -1

    # New user, start onboarding
    await update.message.reply_text(
        "Welcome to Unhooked! 🎯\n\nI'm here to support your digital wellness journey. "
        "Let's get started. What's your name?"
    )
    return REGISTER


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle name registration."""
    user_id = update.effective_user.id
    name = update.message.text.strip()

    if not name or len(name) > 100:
        await update.message.reply_text("Please enter a valid name (1-100 characters).")
        return REGISTER

    context.user_data["name"] = name
    await update.message.reply_text(
        f"Nice to meet you, {name}! 👋\n\n"
        "What's the best emergency contact to reach you (name and phone number)? "
        "This will only be used in crisis situations."
    )
    return EMERGENCY_CONTACT


async def register_emergency_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle emergency contact registration."""
    user_id = update.effective_user.id
    contact = update.message.text.strip()

    if not contact or len(contact) > 500:
        await update.message.reply_text("Please enter a valid contact (1-500 characters).")
        return EMERGENCY_CONTACT

    name = context.user_data.get("name", "User")
    store = Store(DATA_DIR)
    state = UserState(
        user_id=user_id,
        name=name,
        emergency_contact=contact,
        streak_start_date=_today(),
        last_check_in=_today(),
        journal_entries=[],
        total_saved_cents=0,
    )
    store.save_user(user_id, state)

    await update.message.reply_text(
        f"Great! I've saved your emergency contact. 📋\n\n"
        "You're all set. Here's what I can help with:\n\n"
        "• /status - Track your streak\n"
        "• /sos - Crisis toolkit (grounding, breathing, urge surfing)\n"
        "• /journal - Reflect and write\n"
        "• /check - Daily mood check-in\n"
        "• /savings - Celebrate money saved\n"
        "• /reset - Start a new streak\n"
        "• /help - See all commands\n\n"
        "Let's go! 🚀"
    )
    return -1


# ── Streak tracking ──────────────────────────────────────────────────────


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command to show streak and stats."""
    if not await _check_whitelist(update, context):
        return

    user_id = update.effective_user.id
    _log_command(user_id, "status")

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state:
        await update.message.reply_text(
            "You haven't registered yet. Use /start to begin."
        )
        return

    today = _today()
    streak_days = (today - state.streak_start_date).days

    emoji_map = {1: "🔥", 7: "⭐", 14: "👑", 30: "🏆", 100: "💎"}
    emoji = next((v for k, v in sorted(emoji_map.items()) if streak_days >= k), "💪")

    msg = f"{emoji} Streak: {streak_days} days!\n\n" f"You're doing amazing!"
    await update.message.reply_text(msg)


async def reset_streak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reset command to restart streak."""
    if not await _check_whitelist(update, context):
        return

    user_id = update.effective_user.id
    _log_command(user_id, "reset")

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state:
        await update.message.reply_text(
            "You haven't registered yet. Use /start to begin."
        )
        return

    state.streak_start_date = _today()
    store.save_user(user_id, state)

    await update.message.reply_text(
        "Your streak has been reset! 🆕\n\nLet's start fresh. You've got this! 💪"
    )


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /undo command to restore previous streak."""
    if not await _check_whitelist(update, context):
        return

    user_id = update.effective_user.id
    _log_command(user_id, "undo")

    await update.message.reply_text(
        "Undo is not yet implemented. You can use /reset to restart if needed."
    )


# ── Crisis toolkit ───────────────────────────────────────────────────────


async def sos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sos command (crisis toolkit)."""
    if not await _check_whitelist(update, context):
        return

    user_id = update.effective_user.id
    _log_command(user_id, "sos")

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state:
        await update.message.reply_text(
            "You haven't registered yet. Use /start to begin."
        )
        return

    keyboard = [
        [InlineKeyboardButton("Grounding 🌍", callback_data="grounding")],
        [InlineKeyboardButton("Breathing 💨", callback_data="breathing")],
        [InlineKeyboardButton("Urge Surfing 🏄", callback_data="urge_surfing")],
        [InlineKeyboardButton("Call Emergency 📞", callback_data="call_emergency")],
        [InlineKeyboardButton("Distraction 🎮", callback_data="distraction")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Crisis Toolkit 🆘\n\nYou're not alone. Let's work through this together. "
        "Choose a technique:",
        reply_markup=markup,
    )


async def grounding_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send grounding technique (5-4-3-2-1)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="5-4-3-2-1 Grounding Technique 🌍\n\n"
        "Look around and name:\n"
        "• 5 things you can SEE\n"
        "• 4 things you can TOUCH\n"
        "• 3 things you can HEAR\n"
        "• 2 things you can SMELL\n"
        "• 1 thing you can TASTE\n\n"
        "Take your time. You're safe. 💚"
    )


async def breathing_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send non-blocking breathing exercise."""
    query = update.callback_query
    await query.answer()

    steps = [
        "🫁 Box Breathing Exercise\n\n1️⃣ Breathe in for 4 seconds",
        "🫁 Box Breathing Exercise\n\n1️⃣ Breathe in for 4 seconds\n2️⃣ Hold for 4 seconds",
        "🫁 Box Breathing Exercise\n\n1️⃣ Breathe in for 4 seconds\n2️⃣ Hold for 4 seconds\n3️⃣ Breathe out for 4 seconds",
        "🫁 Box Breathing Exercise\n\n1️⃣ Breathe in for 4 seconds\n2️⃣ Hold for 4 seconds\n3️⃣ Breathe out for 4 seconds\n4️⃣ Hold for 4 seconds\n\nRepeat 4 times. You're doing great! 💚",
    ]

    await query.edit_message_text(text=steps[0])

    # Schedule updates without blocking
    for i in range(1, len(steps)):
        await asyncio.sleep(4)
        try:
            await query.edit_message_text(text=steps[i])
        except Exception:
            pass


async def urge_surfing_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send urge surfing technique."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="Urge Surfing 🏄\n\n"
        "Urges are like waves. You can ride them without acting.\n\n"
        "1. NOTICE: What are you feeling? Describe it.\n"
        "2. BREATHE: Take slow, deep breaths.\n"
        "3. NAME IT: 'This is a craving. It will pass.'\n"
        "4. OBSERVE: Watch the urge peak, then fade.\n\n"
        "Most urges last 10-20 minutes. You've got this! 💪"
    )


async def call_emergency_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send emergency contact info."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state or not state.emergency_contact:
        await query.edit_message_text(
            text="Emergency Contact\n\n"
            "You haven't set an emergency contact yet. Use /start to add one."
        )
        return

    await query.edit_message_text(
        text=f"📞 Emergency Contact\n\n{state.emergency_contact}\n\n"
        "Please reach out. Help is available. You're not alone. 💚"
    )


async def distraction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send distraction activities."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="Distraction Activities 🎮\n\n"
        "Try one of these:\n"
        "• Go for a walk or run\n"
        "• Listen to your favorite music\n"
        "• Watch a funny video\n"
        "• Call a friend\n"
        "• Do a creative activity (draw, write, paint)\n"
        "• Play a game\n"
        "• Take a cold shower\n"
        "• Journal your thoughts\n\n"
        "Find something that engages you. You'll get through this! 💪"
    )


# ── Journaling ───────────────────────────────────────────────────────────


async def journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /journal command."""
    if not await _check_whitelist(update, context):
        return -1

    user_id = update.effective_user.id
    _log_command(user_id, "journal")

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state:
        await update.message.reply_text(
            "You haven't registered yet. Use /start to begin."
        )
        return -1

    prompts = [
        "What's on your mind today?",
        "How are you feeling right now?",
        "What's one thing you're proud of today?",
        "What's a challenge you faced and how did you handle it?",
    ]
    prompt = prompts[len(state.journal_entries) % len(prompts)]

    await update.message.reply_text(f"📔 Journal\n\n{prompt}")
    return JOURNAL


async def journal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle journal entry submission."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not text or len(text) > 5000:
        await update.message.reply_text(
            "Please enter a journal entry (1-5000 characters)."
        )
        return JOURNAL

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state:
        await update.message.reply_text("Something went wrong. Please try again.")
        return -1

    state.journal_entries.append({"date": _today().isoformat(), "text": text})
    store.save_user(user_id, state)

    await update.message.reply_text(
        "Thank you for sharing. Your entry has been saved. 💚\n\n"
        "Keep reflecting and growing!"
    )
    return -1


# ── Savings tracking ────────────────────────────────────────────────────


async def savings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /savings command to show progress."""
    if not await _check_whitelist(update, context):
        return

    user_id = update.effective_user.id
    _log_command(user_id, "savings")

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state:
        await update.message.reply_text(
            "You haven't registered yet. Use /start to begin."
        )
        return

    amount = state.total_saved_cents / 100
    msg = f"💰 Total Saved: ${amount:.2f}\n\nGreat work! Keep going! 🚀"
    await update.message.reply_text(msg)


# ── Mood check-in ──────────────────────────────────────────────────────


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /check command for mood check-in."""
    if not await _check_whitelist(update, context):
        return

    user_id = update.effective_user.id
    _log_command(user_id, "check")

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state:
        await update.message.reply_text(
            "You haven't registered yet. Use /start to begin."
        )
        return

    state.last_check_in = _today()
    store.save_user(user_id, state)

    keyboard = [
        [InlineKeyboardButton("😊 Great", callback_data="mood_great")],
        [InlineKeyboardButton("😐 Okay", callback_data="mood_okay")],
        [InlineKeyboardButton("😞 Not good", callback_data="mood_bad")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "How are you feeling right now?", reply_markup=markup
    )


async def mood_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle mood selection."""
    query = update.callback_query
    await query.answer()

    mood_map = {
        "mood_great": ("😊 That's wonderful! Keep shining! ✨", "great"),
        "mood_okay": ("😐 That's fair. One step at a time. 💪", "okay"),
        "mood_bad": (
            "😞 Tough day? Remember, this too shall pass. Reach out if you need support. 💚",
            "bad",
        ),
    }

    message, mood_key = mood_map.get(query.data, ("Thanks for sharing.", "unknown"))
    await query.edit_message_text(text=message)


# ── AI coaching ────────────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-text messages for AI coaching."""
    if not await _check_whitelist(update, context):
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not text or len(text) > 5000:
        return

    store = Store(DATA_DIR)
    state = store.get_user(user_id)

    if not state:
        await update.message.reply_text(
            "You haven't registered yet. Use /start to begin."
        )
        return

    # Show typing indicator
    await update.message.chat.send_action("typing")

    try:
        client = AIClient(AI_PROVIDER, AI_MODEL, OPENAI_KEY, ANTHROPIC_KEY)
        history = History(state.journal_entries)
        intent = detect_intent(text)

        response = await client.coach(text, intent, history)
        await _send_markdown(update.message, response)
    except Exception as e:
        log.error("AI coaching error: %s", e)
        await update.message.reply_text(
            "Sorry, I couldn't process your message. Please try again."
        )


# ── Scheduled messages ────────────────────────────────────────────────


async def schedule_messages(app: Application) -> None:
    """Set up scheduled messages (morning, nudge, evening)."""
    job_queue = app.job_queue

    # Morning message at 8 AM
    job_queue.run_daily(
        send_morning_message,
        time=time(8, 0),
        tzinfo=_get_tz(),
        name="morning_message",
    )

    # Nudge message at 12 PM
    job_queue.run_daily(
        send_nudge_message,
        time=time(12, 0),
        tzinfo=_get_tz(),
        name="nudge_message",
    )

    # Evening message at 6 PM
    job_queue.run_daily(
        send_evening_message,
        time=time(18, 0),
        tzinfo=_get_tz(),
        name="evening_message",
    )


async def send_morning_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send morning motivation."""
    store = Store(DATA_DIR)
    for user_id in store.list_users():
        try:
            await context.bot.send_message(
                user_id,
                "☀️ Good morning! You've got this. Start your day strong! 💪",
            )
        except Exception as e:
            log.debug("Could not send morning message to %s: %s", user_id, e)


async def send_nudge_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send midday nudge."""
    store = Store(DATA_DIR)
    for user_id in store.list_users():
        try:
            await context.bot.send_message(
                user_id,
                "🌤️ How's your day going? Check in with /check to see how you're feeling.",
            )
        except Exception as e:
            log.debug("Could not send nudge to %s: %s", user_id, e)


async def send_evening_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send evening reflection prompt."""
    store = Store(DATA_DIR)
    for user_id in store.list_users():
        try:
            await update_mission_status(context.bot, user_id, store)
        except Exception as e:
            log.debug("Could not send evening message to %s: %s", user_id, e)


async def update_mission_status(
    bot: Any, user_id: int, store: Store
) -> None:  # type: ignore
    """Update mission status in the evening."""
    state = store.get_user(user_id)
    if not state:
        return

    today = _today()
    streak_days = (today - state.streak_start_date).days

    msg = (
        f"🌙 Evening reflection\n\n"
        f"Your streak: {streak_days} days 🔥\n"
        f"Journal entries: {len(state.journal_entries)}\n\n"
        f"Reflect on your day with /journal or just wind down. You did great! 💚"
    )

    try:
        await bot.send_message(user_id, msg)
    except Exception as e:
        log.debug("Could not send evening message: %s", e)


# ── Help and other commands ───────────────────────────────────────────


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if not await _check_whitelist(update, context):
        return

    help_text = """🎯 Unhooked Lite Commands

/start - Begin onboarding or see main menu
/status - Check your current streak
/reset - Restart your streak
/undo - Undo a reset (coming soon)

/sos - Crisis toolkit
  • Grounding (5-4-3-2-1 technique)
  • Breathing (box breathing)
  • Urge surfing
  • Emergency contact
  • Distraction activities

/journal - Write a journal entry
/check - Quick mood check-in
/savings - View total amount saved
/help - Show this menu

You're not alone. We're here to support you! 💚
    """
    await update.message.reply_text(help_text)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unknown commands."""
    await update.message.reply_text(
        "I didn't understand that command. Try /help to see what I can do!"
    )


# ── Bot setup ────────────────────────────────────────────────────────────


async def post_init(app: Application) -> None:
    """Initialize the bot after it starts."""
    log.info("Bot is starting up...")
    await schedule_messages(app)
    log.info("Scheduled messages set up.")


def main() -> None:
    """Start the bot."""
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Onboarding
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            EMERGENCY_CONTACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_emergency_contact)
            ],
            JOURNAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, journal_entry)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv_handler)

    # Streak commands
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset_streak))
    app.add_handler(CommandHandler("undo", undo))

    # Crisis toolkit
    app.add_handler(CommandHandler("sos", sos))
    app.add_handler(
        CallbackQueryHandler(grounding_handler, pattern="^grounding$")
    )
    app.add_handler(
        CallbackQueryHandler(breathing_handler, pattern="^breathing$")
    )
    app.add_handler(
        CallbackQueryHandler(urge_surfing_handler, pattern="^urge_surfing$")
    )
    app.add_handler(
        CallbackQueryHandler(call_emergency_handler, pattern="^call_emergency$")
    )
    app.add_handler(
        CallbackQueryHandler(distraction_handler, pattern="^distraction$")
    )

    # Journaling
    app.add_handler(CommandHandler("journal", journal))

    # Savings
    app.add_handler(CommandHandler("savings", savings))

    # Mood check-in
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(mood_handler, pattern="^mood_"))

    # AI coaching (free-text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Help and unknown
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Start the bot
    log.info("Starting bot...")
    app.run_polling()


if __name__ == "__main__":
    main()
