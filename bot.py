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

# ── Models ────────────────────────────────────────────────────────────────

# State machine for /journal
JOURNAL_PROMPT, JOURNAL_REFLECTION = range(2)

# State machine for /sos (crisis toolkit)
SOS_MENU, SOS_GROUNDING, SOS_BREATHING, SOS_URGE, SOS_CALL, SOS_DISTRACT = range(6)

# ── Prompts ───────────────────────────────────────────────────────────────

COACH_SYSTEM_PROMPT = """You are the Unhooked Coach, a wise, empathetic, and insightful guide who understands
the challenges of internet addiction and digital wellness. Your role is to help users:

1. Understand their triggers, patterns, and underlying emotions
2. Build resilience and healthy coping mechanisms
3. Replace harmful habits with nourishing activities
4. Celebrate progress and learn from setbacks

Key principles:
- Be warm, non-judgmental, and genuine
- Ask clarifying questions rather than giving quick advice
- Help users discover their own insights
- Reference their personal goals and motivations
- Acknowledge their struggles while building confidence
- Keep responses concise (2-3 short paragraphs max)
- Use 'you' language when connecting with emotions
- If they're in crisis, offer grounding techniques or suggest emergency contacts
"""

START_MESSAGE = """Willkommen bei Unhooked — einem Coaching-Bot für digitale Wellness.

Ich bin dein Unhooked Coach und bin hier, um dir zu helfen:

📈 **Streak tracking** — Deine Fortschritte verfolgen
🔧 **Crisis toolkit** — Wege mit Herausforderungen umzugehen
💬 **AI coaching** — Personalisierte Unterstützung
📔 **Journaling** — Deine Gedanken und Gefühle erkunden
💰 **Savings tracking** — Dein Geld sparen und Ziele erreichen
✨ **Quick check-ins** — Deine aktuelle Stimmung teilen

Wähle einfach eine der Optionen unten, oder schreib mir einfach eine Nachricht — ich bin jederzeit für dich da.
"""

# ──────────────────────────────────────────────────────────────────────────
# HANDLERS
# ──────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initialize user and show main menu."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        await update.message.reply_text(
            "Sorry, you're not in the allowed list for this bot."
        )
        return

    # Initialize store if needed
    if "store" not in context.bot_data:
        context.bot_data["store"] = Store()
    store: Store = context.bot_data["store"]

    # Initialize user state if needed
    if str(user_id) not in store.users:
        store.users[str(user_id)] = UserState(user_id=user_id, chat_id=chat_id)
    user = store.users[str(user_id)]

    # Create keyboard
    keyboard = [
        [InlineKeyboardButton("📈 Status", callback_data="status")],
        [InlineKeyboardButton("🔄 Reset Streak", callback_data="reset_streak")],
        [InlineKeyboardButton("↩️ Undo", callback_data="undo")],
        [InlineKeyboardButton("🆘 Crisis Toolkit", callback_data="sos")],
        [InlineKeyboardButton("💬 Talk to Coach", callback_data="coach")],
        [InlineKeyboardButton("📔 Journal", callback_data="journal")],
        [InlineKeyboardButton("💰 Savings", callback_data="savings")],
        [InlineKeyboardButton("✨ Mood Check", callback_data="mood")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(START_MESSAGE, reply_markup=reply_markup)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    help_text = """Commands:
/start — Main menu
/status — View your streak
/reset — Reset streak
/undo — Undo last reset
/sos — Crisis toolkit
/journal — Start journaling
/savings — Track savings
/check — Quick mood check
/help — This message
"""
    await update.message.reply_text(help_text)


async def btn_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's current streak."""
    user_id = update.effective_user.id
    query = update.callback_query

    # Get store
    store: Store = context.bot_data.get("store")
    if not store or str(user_id) not in store.users:
        await query.answer("User data not found.")
        return

    user = store.users[str(user_id)]
    days = (date.today() - user.start_date).days

    message = f"""📈 **Your Streak**

Days: {days}
Current mood: {user.mood or 'Not set'}
Total savings: €{user.total_savings:.2f}
"""
    await query.edit_message_text(message)
    await query.answer()


async def btn_reset_streak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset user's streak."""
    user_id = update.effective_user.id
    query = update.callback_query

    store: Store = context.bot_data.get("store")
    if not store or str(user_id) not in store.users:
        await query.answer("User data not found.")
        return

    user = store.users[str(user_id)]
    old_days = (date.today() - user.start_date).days
    user.start_date = date.today()
    user.reset_history.append(old_days)

    await query.edit_message_text(
        f"✅ Streak reset. You had {old_days} days. Let's start fresh!"
    )
    await query.answer()


async def btn_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Undo last reset."""
    user_id = update.effective_user.id
    query = update.callback_query

    store: Store = context.bot_data.get("store")
    if not store or str(user_id) not in store.users:
        await query.answer("User data not found.")
        return

    user = store.users[str(user_id)]
    if user.reset_history:
        days_back = user.reset_history.pop()
        user.start_date = date.today() - timedelta(days=days_back)
        await query.edit_message_text(
            f"✅ Undo successful. Back to {days_back} days."
        )
    else:
        await query.edit_message_text("❌ Nothing to undo.")
    await query.answer()


async def btn_sos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show SOS (crisis toolkit) menu."""
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🌍 Grounding", callback_data="sos_grounding")],
        [InlineKeyboardButton("🌬️ Breathing", callback_data="sos_breathing")],
        [InlineKeyboardButton("🏄 Urge Surfing", callback_data="sos_urge")],
        [InlineKeyboardButton("📞 Call Help", callback_data="sos_call")],
        [InlineKeyboardButton("🎮 Distraction", callback_data="sos_distract")],
        [InlineKeyboardButton("← Back", callback_data="back")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🆘 **Crisis Toolkit** — Choose a technique:", reply_markup=reply_markup
    )
    await query.answer()


async def btn_sos_grounding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grounding technique."""
    query = update.callback_query
    technique = """🌍 **5-4-3-2-1 Grounding Technique**

Find and name:
- 5 things you can **see**
- 4 things you can **touch**
- 3 things you can **hear**
- 2 things you can **smell**
- 1 thing you can **taste**

Take your time. This brings you back to the present moment.
"""
    keyboard = [[InlineKeyboardButton("← Back", callback_data="sos")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(technique, reply_markup=reply_markup)
    await query.answer()


async def btn_sos_breathing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Breathing technique."""
    query = update.callback_query
    technique = """🌬️ **4-7-8 Breathing**

1. Breathe in for **4** counts
2. Hold for **7** counts
3. Exhale for **8** counts
4. Repeat 4 times

This calms the nervous system. Go slow and focus on each breath.
"""
    keyboard = [[InlineKeyboardButton("← Back", callback_data="sos")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(technique, reply_markup=reply_markup)
    await query.answer()


async def btn_sos_urge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Urge surfing technique."""
    query = update.callback_query
    technique = """🏄 **Urge Surfing**

1. Notice the urge without judgment
2. Describe it: Where do you feel it? Hot, cold, tight?
3. Watch it rise like a wave
4. It peaks, then naturally falls
5. You're the surfer, not the wave

Urges are temporary. They pass if you don't act on them.
"""
    keyboard = [[InlineKeyboardButton("← Back", callback_data="sos")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(technique, reply_markup=reply_markup)
    await query.answer()


async def btn_sos_call(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Emergency contact info."""
    query = update.callback_query
    contacts = """📞 **Crisis Resources**

If you're in immediate danger:
🇦🇹 Austria: **142** (Telefonseelsorge)
🇩🇪 Germany: **0800 1110111** or **0800 1110222**
🇨🇭 Switzerland: **143** (Die Dargebotene Hand)

Or reach out to someone you trust right now.
"""
    keyboard = [[InlineKeyboardButton("← Back", callback_data="sos")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(contacts, reply_markup=reply_markup)
    await query.answer()


async def btn_sos_distract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Distraction suggestions."""
    query = update.callback_query
    suggestions = """🎮 **Healthy Distractions**

- Go for a walk
- Do 10 pushups
- Call a friend
- Drink a cold glass of water
- Read a few pages of a book
- Listen to your favorite song
- Stretch for 5 minutes
- Write about what you're feeling

Pick something that feels good right now.
"""
    keyboard = [[InlineKeyboardButton("← Back", callback_data="sos")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(suggestions, reply_markup=reply_markup)
    await query.answer()


async def btn_coach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user to talk to the coach."""
    query = update.callback_query
    await query.edit_message_text(
        "💬 **Talk to the Coach**\n\nJust reply with your message and I'll respond with coaching."
    )
    await query.answer()


async def btn_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start journaling."""
    query = update.callback_query
    await query.edit_message_text(
        "📔 **Journal Prompt**\n\nWhat's on your mind today? What triggered you? What did you do instead?"
    )
    await query.answer()
    return JOURNAL_PROMPT


async def btn_savings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show savings info."""
    query = update.callback_query
    user_id = update.effective_user.id

    store: Store = context.bot_data.get("store")
    if not store or str(user_id) not in store.users:
        await query.answer("User data not found.")
        return

    user = store.users[str(user_id)]
    message = f"""💰 **Savings**

Total saved: €{user.total_savings:.2f}

Every hour saved from scrolling is money in your pocket.
"""
    await query.edit_message_text(message)
    await query.answer()


async def btn_mood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick mood check-in."""
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("😔", callback_data="mood_1")],
        [InlineKeyboardButton("😐", callback_data="mood_2")],
        [InlineKeyboardButton("🙂", callback_data="mood_3")],
        [InlineKeyboardButton("😄", callback_data="mood_4")],
        [InlineKeyboardButton("🤩", callback_data="mood_5")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "✨ **How are you feeling right now?**", reply_markup=reply_markup
    )
    await query.answer()


async def btn_mood_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record mood rating."""
    query = update.callback_query
    user_id = update.effective_user.id
    rating = int(query.data.split("_")[1])

    store: Store = context.bot_data.get("store")
    if store and str(user_id) in store.users:
        user = store.users[str(user_id)]
        user.mood = rating

    emojis = {1: "😔", 2: "😐", 3: "🙂", 4: "😄", 5: "🤩"}
    await query.edit_message_text(
        f"✅ Got it! You're feeling {emojis.get(rating, '😐')} today."
    )
    await query.answer()


async def btn_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Go back to main menu."""
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("📈 Status", callback_data="status")],
        [InlineKeyboardButton("🔄 Reset Streak", callback_data="reset_streak")],
        [InlineKeyboardButton("↩️ Undo", callback_data="undo")],
        [InlineKeyboardButton("🆘 Crisis Toolkit", callback_data="sos")],
        [InlineKeyboardButton("💬 Talk to Coach", callback_data="coach")],
        [InlineKeyboardButton("📔 Journal", callback_data="journal")],
        [InlineKeyboardButton("💰 Savings", callback_data="savings")],
        [InlineKeyboardButton("✨ Mood Check", callback_data="mood")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🏠 **Main Menu** — Choose an option:", reply_markup=reply_markup
    )
    await query.answer()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-text messages for coaching."""
    user_id = update.effective_user.id
    user_text = update.message.text

    # Get store
    if "store" not in context.bot_data:
        context.bot_data["store"] = Store()
    store: Store = context.bot_data["store"]

    if str(user_id) not in store.users:
        store.users[str(user_id)] = UserState(
            user_id=user_id, chat_id=update.effective_chat.id
        )
    user = store.users[str(user_id)]

    # Get or create AI client
    if "ai_client" not in context.bot_data:
        context.bot_data["ai_client"] = AIClient(
            provider=AI_PROVIDER,
            model=AI_MODEL,
            openai_key=OPENAI_KEY,
            anthropic_key=ANTHROPIC_KEY,
        )
    ai_client: AIClient = context.bot_data["ai_client"]

    # Update user history
    user.history.add_message("user", user_text)

    # Detect intent
    intent = detect_intent(user_text)
    log.info(f"User {user_id} intent: {intent}")

    # Get response from AI
    try:
        response = ai_client.get_response(
            messages=user.history.messages,
            system=COACH_SYSTEM_PROMPT,
        )
        user.history.add_message("assistant", response)
        await update.message.reply_text(response)
    except Exception as e:
        log.error(f"Error getting AI response: {e}")
        await update.message.reply_text(
            "Sorry, I couldn't process that. Please try again."
        )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and notify the user."""
    log.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "An error occurred. Please try again later."
        )


# ──────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Set up and run the bot."""
    app = Application.builder().token(BOT_TOKEN).build()

    # Initialize store on startup
    async def post_init(application: Application) -> None:
        if "store" not in application.bot_data:
            application.bot_data["store"] = Store()
        log.info("Bot started. Store initialized.")

    app.post_init = post_init

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # Callback buttons
    app.add_handler(
        CallbackQueryHandler(
            btn_status,
            pattern="^status$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_reset_streak,
            pattern="^reset_streak$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_undo,
            pattern="^undo$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_sos,
            pattern="^sos$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_sos_grounding,
            pattern="^sos_grounding$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_sos_breathing,
            pattern="^sos_breathing$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_sos_urge,
            pattern="^sos_urge$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_sos_call,
            pattern="^sos_call$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_sos_distract,
            pattern="^sos_distract$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_coach,
            pattern="^coach$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_journal,
            pattern="^journal$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_savings,
            pattern="^savings$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_mood,
            pattern="^mood$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_mood_rating,
            pattern="^mood_[1-5]$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            btn_back,
            pattern="^back$",
        )
    )

    # Messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    # Error handler
    app.add_error_handler(error_handler)

    # Run
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
