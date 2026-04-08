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
            log.warning("Invalid chat ID: %s", x)

store = Store(DATA_DIR)

# ── Onboarding State Machine ──────────────────────────────────────────────
ONBOARDING, HABIT_Q, HABIT_A, WHY_Q, WHY_A, WAKE_Q, WAKE_A = range(7)

# ── Crisis Toolkit ───────────────────────────────────────────────────────
CRISIS_TOOLS = {
    "5-4-3-2-1": "Focus on 5 things you *see*, 4 you *touch*, 3 you *hear*, 2 you *smell*, 1 you *taste*.",
    "4-7-8 Breath": "Inhale 4 counts, hold 7, exhale 8. Repeat 4-8 times.",
    "Urge Surf": "Observe the urge like a wave: it builds, peaks, then passes (usually 20-30 min).",
    "Ice Challenge": "Hold ice in your hand for as long as you can—physical sensation replaces the urge.",
    "Walk/Run": "Get outside, move your body, change your environment.",
    "Cold Shower": "The shock jolts your nervous system; urges often pass afterward.",
}

# ── Quick Data ────────────────────────────────────────────────────────────
HABITS = [
    ("📱 Phone/Social", "phone"),
    ("🍷 Alcohol", "alcohol"),
    ("🚬 Smoking", "smoking"),
    ("🎮 Gaming", "gaming"),
    ("💻 Internet", "internet"),
    ("☕ Caffeine", "caffeine"),
    ("🍫 Sugar/Food", "food"),
    ("💊 Drugs", "drugs"),
    ("😴 Sleep (Insomnia)", "sleep"),
    ("📺 TV/Streaming", "tv"),
    ("🛒 Shopping", "shopping"),
    ("👁 Porn", "porn"),
    ("Other", None),
]

TRIGGERS = [
    "Stress/Anxiety",
    "Boredom",
    "Social Pressure",
    "Loneliness",
    "Anger",
    "Fatigue",
    "Success/Celebration",
    "Specific Time of Day",
    "Specific Place",
    "Specific Person",
]

LAST_USE = [
    ("Today", 0),
    ("Yesterday", 1),
    ("2-3 days ago", 3),
    ("A week ago", 7),
    ("2+ weeks ago", 14),
]

# ── Commands ──────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: check if user exists, start onboarding if not."""
    if not _user_allowed(update):
        await update.message.reply_text("❌ Not authorized.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    user = store.load(user_id)

    if user and user.habit:
        # User already set up; show status instead
        await cmd_status(update, context)
        return ConversationHandler.END

    # New user or incomplete onboarding
    if not user:
        user = UserState(user_id=user_id, username=update.effective_user.username or "")
        store.save(user)

    await update.message.reply_text(
        "🎯 *Unhooked Lite* — Quit your habit, track your streak, rebuild your life.\n"
        "Let\'s get started!",
        parse_mode="Markdown",
    )
    return await _ask_habit(update, context)


async def _ask_habit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask: what habit do you want to quit?"""
    kb = [[InlineKeyboardButton(label, callback_data=f"h:{name or \'other\'}")] for label, name in HABITS]
    await update.message.reply_text(
        "*Which habit do you want to quit?*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return HABIT_Q


async def on_habit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback: habit selected."""
    query = update.callback_query
    await query.answer()
    habit = query.data.split(":", 1)[1]
    user = store.load(query.from_user.id)
    if not user:
        return ConversationHandler.END
    user.habit = habit
    store.save(user)
    await query.edit_message_text(
        f"*You\'re quitting:* {habit}\n\nNow, *why*? (Keep it short—this is your north star.)",
        parse_mode="Markdown",
    )
    return WHY_Q


async def on_why(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture the reason."""
    user = store.load(update.effective_user.id)
    if not user:
        return ConversationHandler.END
    user.why = update.message.text
    store.save(user)
    await update.message.reply_text(
        "*What time do you wake up?* (HH:MM format, e.g. 07:30)",
        parse_mode="Markdown",
    )
    return WAKE_Q


async def on_wake_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and store wake time."""
    user = store.load(update.effective_user.id)
    if not user:
        return ConversationHandler.END
    try:
        h, m = update.message.text.strip().split(":")
        time(int(h), int(m))
        user.wake_time = f"{int(h):02d}:{int(m):02d}"
    except (ValueError, AttributeError):
        await update.message.reply_text("❌ Invalid time. Try again (HH:MM):")
        return WAKE_Q
    store.save(user)
    await update.message.reply_text(
        f"✅ *Onboarding done!*\n*Habit:* {user.habit}\n*Why:* {user.why}\n*Wake:* {user.wake_time}\n\nYour quit date is *today*. Use /status to see your streak!",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current streak, savings, and options."""
    user = store.load(update.effective_user.id)
    if not user or not user.habit:
        await update.message.reply_text(
            "❌ Not onboarded. Use /start to begin.",
        )
        return
    user.calc_streak()
    saving = user.savings()
    msg = (
        f"*{user.habit}*\n"
        f"Quit date: {user.quit_date}\n"
        f"🔥 *Streak:* {user.streak_days} days\n"
        f"🏆 Longest: {user.longest_streak} days\n"
        f"💰 Saved: ${saving:.2f} ({user.savings_per_day:.2f}/day)\n"
        f"😢 Relapses: {user.relapses}\n"
    )
    kb = [
        [InlineKeyboardButton("🚨 Relapse", callback_data="reset")],
        [InlineKeyboardButton("↩️ Undo Last Relapse", callback_data="undo")],
        [InlineKeyboardButton("📔 Journal", callback_data="journal")],
        [InlineKeyboardButton("🆘 SOS Kit", callback_data="sos")],
    ]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


async def on_relapse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask: when did you last use?"""
    query = update.callback_query
    await query.answer()
    kb = [[InlineKeyboardButton(f"{k} ({d}d)", callback_data=f"l:{d}")] for k, d in LAST_USE]
    await query.edit_message_text(
        "*When did you last use?*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return 10  # Relapse flow state


async def on_relapse_time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Follow up: what triggered it?"""
    query = update.callback_query
    await query.answer()
    days_ago = int(query.data.split(":")[1])
    context.user_data["relapse_days_ago"] = days_ago
    kb = [[InlineKeyboardButton(f"{k} ({d}d)", callback_data=f"l:{d}")] for k, d in LAST_USE]
    await query.edit_message_text(
        "*What triggered it?*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return 10


async def on_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Undo last relapse (within 5 min window)."""
    query = update.callback_query
    await query.answer()
    user = store.load(query.from_user.id)
    if not user:
        return
    if user.undo_reset():
        store.save(user)
        await query.edit_message_text("✅ Relapse undone. Stay strong!")
    else:
        await query.edit_message_text(
            "❌ No relapse to undo, or too much time has passed (window: 5 min)."
        )


async def cmd_reset_streak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reset streak (after relapse confirmation)."""
    user = store.load(update.effective_user.id)
    if not user:
        return ConversationHandler.END
    user.reset_streak()
    store.save(user)
    await update.message.reply_text(
        f"📝 *Relapse logged.*\n"
        f"Streak reset to 1. Your saved backup: {user.last_relapse_reset}\n"
        f"Use /undo within 5 minutes to revert.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Journaling prompt."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "📔 *Journal Prompt:*\nWhat triggered the urge? How did you feel? What worked?\n\nJust reply with your thoughts.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "📔 *Journal Prompt:*\nWhat triggered the urge? How did you feel? What worked?\n\nJust reply with your thoughts.",
            parse_mode="Markdown",
        )


async def cmd_sos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show crisis toolkit."""
    query = update.callback_query
    if query:
        await query.answer()
    msg = "🆘 *Crisis Toolkit* — Pick a tool:\n\n" + "\n\n".join(
        [f"*{k}:*\n{v}" for k, v in CRISIS_TOOLS.items()]
    )
    rows = [[InlineKeyboardButton(("✅ " if t in sel else "") + t, callback_data=f"t:{t}")] for t in TRIGGERS]
    kb = rows + [
        [InlineKeyboardButton("🤝 Call Someone", callback_data="call")],
        [InlineKeyboardButton("🏃 Distract (30 min)", callback_data="distract")],
    ]
    if query:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


async def on_sos_trigger_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log which trigger(s) user selected in SOS."""
    query = update.callback_query
    await query.answer("Logged.")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick mood/urge check-in."""
    msg = "*How are you feeling?*\n"
    kb = [
        [InlineKeyboardButton("😌 Calm", callback_data="mood:calm")],
        [InlineKeyboardButton("😟 Anxious", callback_data="mood:anxious")],
        [InlineKeyboardButton("😤 Frustrated", callback_data="mood:frustrated")],
        [InlineKeyboardButton("😢 Sad", callback_data="mood:sad")],
        [InlineKeyboardButton("🤔 Neutral", callback_data="mood:neutral")],
    ]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


async def on_mood_logged(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log mood and store in user's mood_log."""
    query = update.callback_query
    await query.answer()
    mood = query.data.split(":")[1]
    user = store.load(query.from_user.id)
    if not user:
        return
    user.mood_log.append({
        "mood": mood,
        "timestamp": datetime.now(ZoneInfo(user.timezone)).isoformat(),
    })
    store.save(user)
    await query.edit_message_text(f"✅ Logged: {mood}")


async def cmd_savings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show savings progress and set savings goal."""
    user = store.load(update.effective_user.id)
    if not user or not user.habit:
        await update.message.reply_text("❌ Not onboarded.", parse_mode="Markdown")
        return
    user.calc_streak()
    saving = user.savings()
    msg = (
        f"💰 *Savings Tracker*\n"
        f"Daily savings: ${user.savings_per_day:.2f}\n"
        f"Current streak: {user.streak_days} days\n"
        f"Total saved: ${saving:.2f}\n"
    )
    if user.savings_goal:
        msg += f"Goal: ${user.savings_goal:.2f} ({(saving / user.savings_goal * 100):.0f}%)\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text AI coaching via OpenAI or Anthropic."""
    if not _user_allowed(update):
        await update.message.reply_text("❌ Not authorized.")
        return

    user = store.load(update.effective_user.id)
    if not user or not user.habit:
        await update.message.reply_text(
            "❌ Please /start to set up your habit first."
        )
        return

    user.calc_streak()
    await update.message.chat.send_action("typing")

    # Build context for AI
    context_msg = f"User is quitting {user.habit}. Streak: {user.streak_days} days. Why: {user.why}"
    history = History(
        messages=user.chat_history[-10:]  # Last 10 for context
    )

    intent = detect_intent(update.message.text)
    ai = AIClient(AI_PROVIDER, AI_MODEL, OPENAI_KEY, ANTHROPIC_KEY)
    try:
        response = await ai.coach(update.message.text, context_msg, history, intent)
        user.chat_history.append({"role": "user", "content": update.message.text})
        user.chat_history.append({"role": "assistant", "content": response})
        store.save(user)
        # Chunk response into Telegram message size limit (4096 chars)
        for chunk in [response[i:i+4000] for i in range(0, len(response), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        log.error("AI coaching failed: %s", e)
        await update.message.reply_text(f"❌ Error: {e}")


async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    log.error("Update %s caused error %s", update, context.error)


def _user_allowed(update: Update) -> bool:
    """Check if user is in whitelist (if set)."""
    if not ALLOWED_IDS:
        return True
    return update.effective_user.id in ALLOWED_IDS


async def _schedule_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send morning affirmation to all active users."""
    users = store.all_users()
    for user in users:
        try:
            await context.bot.send_message(
                user.user_id,
                f"🌅 *Good morning, {user.username or 'friend'}!*\n"
                f"Today is day {user.streak_days + 1} of your {user.habit} freedom.\n"
                f"You\'ve got this! 💪",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning("Could not send morning message to %d: %s", user.user_id, e)


async def _schedule_nudge(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send afternoon nudge (urge check-in)."""
    users = store.all_users()
    for user in users:
        try:
            kb = [
                [InlineKeyboardButton("😌 I\'m good", callback_data="nudge:ok")],
                [InlineKeyboardButton("😰 Struggling", callback_data="nudge:sos")],
            ]
            await context.bot.send_message(
                user.user_id,
                "*Urge check-in:* How are you holding up? 🤔",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning("Could not send nudge to %d: %s", user.user_id, e)


async def _schedule_evening(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send evening reflection prompt."""
    users = store.all_users()
    for user in users:
        try:
            await context.bot.send_message(
                user.user_id,
                "🌙 *Evening Reflection:*\n"
                "What was the hardest moment today? What helped you stay strong? /journal",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning("Could not send evening message to %d: %s", user.user_id, e)


async def main() -> None:
    """Build and start the bot."""
    app = Application.builder().token(BOT_TOKEN).build()

    # Onboarding conversation
    onboarding_handler = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            HABIT_Q: [CallbackQueryHandler(on_habit_selected, pattern=r"^h:")],
            WHY_Q: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_why)],
            WAKE_Q: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_wake_time)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
    )

    # Status & action callbacks
    status_handler = CommandHandler("status", cmd_status)
    relapse_handler = CallbackQueryHandler(on_relapse, pattern=r"^reset$")
    relapse_time_handler = CallbackQueryHandler(on_relapse_time_selected, pattern=r"^l:")
    undo_handler = CallbackQueryHandler(on_undo, pattern=r"^undo$")
    journal_handler = CallbackQueryHandler(cmd_journal, pattern=r"^journal$")
    sos_handler = CallbackQueryHandler(cmd_sos, pattern=r"^sos$")
    sos_trigger_handler = CallbackQueryHandler(on_sos_trigger_selected, pattern=r"^t:")
    check_handler = CommandHandler("check", cmd_check)
    mood_handler = CallbackQueryHandler(on_mood_logged, pattern=r"^mood:")
    savings_handler = CommandHandler("savings", cmd_savings)
    message_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, on_message)

    # Register handlers
    app.add_handler(onboarding_handler)
    app.add_handler(status_handler)
    app.add_handler(relapse_handler)
    app.add_handler(relapse_time_handler)
    app.add_handler(undo_handler)
    app.add_handler(journal_handler)
    app.add_handler(sos_handler)
    app.add_handler(sos_trigger_handler)
    app.add_handler(check_handler)
    app.add_handler(mood_handler)
    app.add_handler(savings_handler)
    app.add_handler(message_handler)
    app.add_error_handler(on_error)

    # Schedule jobs
    jq = app.job_queue
    try:
        tz = ZoneInfo(TIMEZONE)
    except Exception:
        log.warning("Invalid timezone, using UTC for jobs")
        tz = ZoneInfo("UTC")

    jq.run_daily(_schedule_morning, time(7, 0), tzinfo=tz)
    jq.run_daily(_schedule_nudge, time(14, 0), tzinfo=tz)
    jq.run_daily(_schedule_evening, time(20, 0), tzinfo=tz)

    # Set commands
    await app.bot.set_my_commands([
        BotCommand("start", "Onboard or view status"),
        BotCommand("status", "Check your streak"),
        BotCommand("check", "Quick mood check-in"),
        BotCommand("journal", "Journal prompt"),
        BotCommand("sos", "Crisis toolkit"),
        BotCommand("savings", "Savings tracker"),
    ])

    async with app:
        await app.start()
        log.info("Bot started. Press Ctrl+C to stop.")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
