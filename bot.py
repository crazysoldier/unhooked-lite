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
import random
from datetime import date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

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
ALLOWED_IDS: set[int] = {int(x) for x in _raw_ids.split(",") if x.strip()} if _raw_ids.strip() else set()

# ── Helpers ───────────────────────────────────────────────────────────────

def store(ctx: ContextTypes.DEFAULT_TYPE) -> Store:
    return ctx.bot_data["store"]

def ai(ctx: ContextTypes.DEFAULT_TYPE) -> AIClient:
    return ctx.bot_data["ai"]

def hist(ctx: ContextTypes.DEFAULT_TYPE) -> History:
    return ctx.bot_data["history"]

def _parse_time(s: str) -> time:
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except Exception:
        return time(7, 30)

# ── Security ──────────────────────────────────────────────────────────────

async def _guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_IDS:
        return
    chat = update.effective_chat
    if chat and chat.id in ALLOWED_IDS:
        return
    if not ctx.chat_data.get("denied"):
        msg = update.effective_message
        if msg:
            await msg.reply_text("Private Beta. Zugang noch nicht freigeschaltet.")
        ctx.chat_data["denied"] = True
    raise ApplicationHandlerStop

# ══════════════════════════════════════════════════════════════════════════
# ONBOARDING  (/start)
# ══════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — onboarding flow."""
    if not isinstance(update.effective_user, Update):
        await update.message.reply_text("Whoops, system error.")
        return
    uid = update.effective_user.id
    user = store(ctx).load(uid)
    if user:
        await update.message.reply_text(f"👋 Willkommen zurück, {user.name}!")
        return
    # New user
    ctx.user_data = ctx.user_data or {}
    ctx.user_data["uid"] = uid
    await update.message.reply_text("👋 Willkommen bei Unhooked Lite.\n\nWie heißt du?")

async def on_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Save user name and create account."""
    if not update.message or not update.message.text:
        return
    name = update.message.text.strip()
    uid = ctx.user_data.get("uid") if ctx.user_data else 0
    if not uid:
        await update.message.reply_text("Fehler. Bitte /start erneut.")
        return
    user = UserState(user_id=uid, name=name)
    store(ctx).save(user)
    ctx.user_data = {}  # reset
    await update.message.reply_text(
        f"Gut, {name}! 👋\n\nUnhooked Lite ist dein Begleiter für:\n"
        "✅ Streak tracking\n"
        "✅ Crisis toolkit (/sos)\n"
        "✅ AI coaching\n"
        "✅ Journaling\n\n"
        "Starten wir! /sos für Soforthilfe, oder schreib mir eine Nachricht."
    )

# ══════════════════════════════════════════════════════════════════════════
# STREAK & STATUS
# ══════════════════════════════════════════════════════════════════════════

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current streak, savings, mood."""
    if not update.effective_user:
        return
    uid = update.effective_user.id
    user = store(ctx).load(uid)
    if not user:
        await update.message.reply_text("Starten Sie mit /start.")
        return
    days = (date.today() - user.start_date).days if user.start_date else 0
    savings = user.daily_savings * days if user.daily_savings else 0
    mood_str = f"Mood: {user.last_mood}" if user.last_mood else "Mood: ?"
    text = f"📊 Status\n\n🔥 Streak: {days} days\n💰 Saved: €{savings:.2f}\n{mood_str}"
    await update.message.reply_text(text)

async def reset_streak(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset streak to 0."""
    if not update.effective_user:
        return
    uid = update.effective_user.id
    user = store(ctx).load(uid)
    if not user:
        await update.message.reply_text("Start with /start.")
        return
    # Save old date for undo
    ctx.user_data = ctx.user_data or {}
    ctx.user_data["last_start_date"] = user.start_date
    user.start_date = date.today()
    store(ctx).save(user)
    await update.message.reply_text("🔄 Streak zurückgesetzt.")

async def undo_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Undo last reset."""
    if not update.effective_user:
        return
    uid = update.effective_user.id
    user = store(ctx).load(uid)
    if not user or not (ctx.user_data and ctx.user_data.get("last_start_date")):
        await update.message.reply_text("Nichts zum rückgängig machen.")
        return
    user.start_date = ctx.user_data["last_start_date"]
    store(ctx).save(user)
    ctx.user_data["last_start_date"] = None
    await update.message.reply_text("↩️ Reset rückgängig gemacht.")

# ══════════════════════════════════════════════════════════════════════════
# CRISIS TOOLKIT (/sos)
# ══════════════════════════════════════════════════════════════════════════

C_MENU, C_G1, C_G2, C_G3, C_G4, C_G5, C_SURF1, C_SURF2, C_CONTACT = range(9)

def _sos_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧘 Grounding", callback_data="sos:ground"),
         InlineKeyboardButton("🫁 Atmen", callback_data="sos:breathe")],
        [InlineKeyboardButton("🌊 Urge Surfing", callback_data="sos:surf"),
         InlineKeyboardButton("📞 Anrufen", callback_data="sos:call")],
        [InlineKeyboardButton("🎯 Distraction", callback_data="sos:distract")],
    ])

def _fb_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ja", callback_data="sos:yes"),
         InlineKeyboardButton("❌ Nein", callback_data="sos:no")],
    ])

async def sos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Start /sos crisis toolkit."""
    await update.message.reply_text(
        "🆘 Crisis Kit — Wähle ein Werkzeug:",
        reply_markup=_sos_kb()
    )
    return C_MENU

async def sos_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data or not isinstance(q.message, Message):
        return ConversationHandler.END
    await q.answer()
    action = q.data.split(":")[1]
    msg = q.message
    uid = update.effective_user.id if update.effective_user else 0

    if action == "ground":
        await msg.reply_text("🧘 Grounding (5-4-3-2-1)\n\nNenne mir 5 Dinge, die du gerade SIEHST:")
        return C_G1
    if action == "breathe":
        asyncio.create_task(_run_breathing(msg))
        return C_MENU
    if action == "surf":
        await msg.reply_text("🌊 Urge Surfing\n\nSchritt 1: Wo spürst du das Craving im Körper?\n(z.B. Brust, Magen, Hände)")
        return C_SURF1
    if action == "call":
        # Check persisted UserState first, fall back to in-memory
        contact = None
        if update.effective_user:
            user = store(ctx).load(update.effective_user.id)
            if user and user.emergency_contact:
                contact = user.emergency_contact
        if not contact:
            contact = (ctx.user_data or {}).get("emergency_contact")
        if contact:
            await msg.reply_text(f"Dein Notfallkontakt: {contact}\n\nRuf jetzt an.")
            await msg.reply_text("Hat das geholfen?", reply_markup=_fb_kb())
            return C_MENU
        await msg.reply_text("Schick mir einen Namen oder eine Nummer:")
        return C_CONTACT
    if action == "distract":
        tips = {
            range(5, 12): "🚶 15 Min spazieren | 🧘 Yoga | 🚿 Kalt duschen",
            range(12, 17): "💪 Workout | 🍳 Kochen | 🧹 Aufräumen",
            range(17, 22): "📖 Lesen | 🎧 Podcast | 🤸 Stretching",
        }
        h = datetime.now().hour
        tip = next((v for r, v in tips.items() if h in r), "🫁 Atemübung | 🍵 Tee | 📝 Journaling")
        await msg.reply_text(tip)
        await msg.reply_text("Hat das geholfen?", reply_markup=_fb_kb())
        return C_MENU
    if action == "yes":
        await msg.reply_text("Stark. Du hast das geschafft. 💪")
        return ConversationHandler.END
    if action == "no":
        await msg.reply_text("Probier ein anderes Werkzeug:", reply_markup=_sos_kb())
        return C_MENU
    return C_MENU

async def _run_breathing(msg: Message) -> None:
    m = await msg.reply_text("🫁 Atemübung startet...")
    await asyncio.sleep(2)
    for i in range(1, 6):
        try:
            await m.edit_text(f"Runde {i}/5\n\n🫁 Einatmen... 4s")
            await asyncio.sleep(4)
            await m.edit_text(f"Runde {i}/5\n\n⏸️ Halten... 2s")
            await asyncio.sleep(2)
            await m.edit_text(f"Runde {i}/5\n\n💨 Ausatmen... 6s")
            await asyncio.sleep(6)
        except Exception:
            break
    try:
        await m.edit_text("✅ 5 Runden geschafft. Spür nach, wie sich dein Körper anfühlt.")
    except Exception:
        pass
    await msg.reply_text("Hat das geholfen?", reply_markup=_fb_kb())

# Grounding steps
async def _g_step(update, next_s, prompt):
    if isinstance(update.effective_message, Message):
        await update.effective_message.reply_text(prompt)
    return next_s

async def g1(u, c): return await _g_step(u, C_G2, "👂 Gut. 4 Dinge, die du HÖRST:")
async def g2(u, c): return await _g_step(u, C_G3, "✋ 3 Dinge, die du FÜHLST/BERÜHRST:")
async def g3(u, c): return await _g_step(u, C_G4, "👃 2 Dinge, die du RIECHST:")
async def g4(u, c): return await _g_step(u, C_G5, "👅 1 Ding, das du SCHMECKST:")
async def g5(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if isinstance(update.effective_message, Message):
        await update.effective_message.reply_text("Gut gemacht. Du bist hier. Du bist sicher. 💚")
        await update.effective_message.reply_text("Hat das geholfen?", reply_markup=_fb_kb())
    return C_MENU

# Urge surfing
async def s1(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if isinstance(update.effective_message, Message):
        await update.effective_message.reply_text("Schritt 2: Wie intensiv (0-10)?")
    return C_SURF2

async def s2(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if isinstance(update.effective_message, Message):
        await update.effective_message.reply_text(
            "Gut. Stell dir vor, die Welle steigt, erreicht ihren Höhepunkt und fällt ab.\n"
            "Es kommt vorbei. Du bist stärker. ⛵"
        )
        await update.effective_message.reply_text("Hat das geholfen?", reply_markup=_fb_kb())
    return C_MENU

# Emergency contact
async def on_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not isinstance(update.effective_message, Message):
        return ConversationHandler.END
    contact = (update.effective_message.text or "").strip()
    if not contact:
        await update.effective_message.reply_text("Bitte schick mir einen Namen oder Nummer:")
        return C_CONTACT
    ud = ctx.user_data or {}
    ud["emergency_contact"] = contact
    # Persist to UserState for restart resilience
    if update.effective_user:
        user = store(ctx).load(update.effective_user.id)
        if user:
            user.emergency_contact = contact
            store(ctx).save(user)
    await update.effective_message.reply_text(f"✅ Notfallkontakt gespeichert: {contact}")
    await update.effective_message.reply_text("Hat das geholfen?", reply_markup=_fb_kb())
    return C_MENU

# ══════════════════════════════════════════════════════════════════════════
# AI COACHING (free-text messages)
# ══════════════════════════════════════════════════════════════════════════

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.message.text:
        return
    text = update.message.text
    uid = update.effective_user.id
    user = store(ctx).load(uid)
    if not user:
        await update.message.reply_text("Starte zuerst mit /start.")
        return

    # Intent detection
    intent = detect_intent(text)
    if intent:
        await update.message.reply_text(f"Intent: {intent}")
        return

    # AI response
    try:
        resp = await ai(ctx).chat(text, user)
        await update.message.reply_text(resp)
        hist(ctx).add(uid, "user", text)
        hist(ctx).add(uid, "assistant", resp)
    except Exception as e:
        log.error("AI error: %s", e)
        await update.message.reply_text("AI Fehler. Versuchen Sie es später.")

# ══════════════════════════════════════════════════════════════════════════
# SAVINGS
# ══════════════════════════════════════════════════════════════════════════

async def savings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show savings or set goal."""
    if not update.effective_user:
        return
    uid = update.effective_user.id
    user = store(ctx).load(uid)
    if not user:
        await update.message.reply_text("Start with /start.")
        return
    if not user.daily_savings:
        text = "Kein Sparziel gesetzt. Antworte: /savings 10 (für €10/Tag)"
    else:
        days = (date.today() - user.start_date).days if user.start_date else 0
        total = user.daily_savings * days
        text = f"💰 Gespart: €{total:.2f}\n📅 {days} Tage à €{user.daily_savings}"
    await update.message.reply_text(text)

async def on_savings_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set daily savings amount."""
    if not update.message or not update.message.text or not update.effective_user:
        return
    try:
        text = update.message.text.strip()
        parts = text.split()
        amount = float(parts[-1])
        uid = update.effective_user.id
        user = store(ctx).load(uid)
        if user:
            user.daily_savings = amount
            store(ctx).save(user)
            await update.message.reply_text(f"✅ Sparziel: €{amount}/Tag gespeichert.")
    except Exception:
        await update.message.reply_text("Format: /savings 10")

# ══════════════════════════════════════════════════════════════════════════
# JOURNALING
# ══════════════════════════════════════════════════════════════════════════

async def journal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start journaling."""
    await update.message.reply_text(
        "📝 Journal\n\n"
        "Schreib einen Eintrag, oder:\n"
        "/journal list — Alle Einträge\n"
        "/journal 1 — Eintrag 1 lesen"
    )

async def on_journal_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Save journal entry."""
    if not update.message or not update.message.text or not update.effective_user:
        return
    uid = update.effective_user.id
    user = store(ctx).load(uid)
    if not user:
        return
    # Simple storage: append to list
    if not user.journal_entries:
        user.journal_entries = []
    user.journal_entries.append({
        "date": str(date.today()),
        "text": update.message.text
    })
    store(ctx).save(user)
    await update.message.reply_text(f"✅ Eintrag #{len(user.journal_entries)} gespeichert.")

# ══════════════════════════════════════════════════════════════════════════
# QUICK CHECK-IN (/check)
# ══════════════════════════════════════════════════════════════════════════

async def check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick mood check-in."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("😊", callback_data="mood:great"),
         InlineKeyboardButton("😐", callback_data="mood:ok"),
         InlineKeyboardButton("😢", callback_data="mood:bad")],
    ])
    await update.message.reply_text("Wie geht es dir?", reply_markup=kb)

async def on_mood(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Save mood."""
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    mood = q.data.split(":")[1]
    uid = update.effective_user.id if update.effective_user else 0
    user = store(ctx).load(uid)
    if user:
        user.last_mood = mood
        store(ctx).save(user)
    await q.message.reply_text(f"✅ Mood gespeichert: {mood}")

# ══════════════════════════════════════════════════════════════════════════
# SCHEDULED MESSAGES (morning, nudge, evening)
# ══════════════════════════════════════════════════════════════════════════

async def _send_morning(app: Application, uid: int) -> None:
    """Send morning briefing (scheduled)."""
    try:
        user = store(app.bot_data["store"]).load(uid)
        if not user:
            return
        msg = f"🌅 Guten Morgen, {user.name}!\n\nStreik: {(date.today() - user.start_date).days if user.start_date else 0} Tage 🔥"
        await app.bot.send_message(uid, msg)
    except Exception as e:
        log.error("Morning message error: %s", e)

async def _send_nudge(app: Application, uid: int) -> None:
    """Midday nudge (scheduled)."""
    try:
        msg = "💪 Wie geht's? Komm zur /check."
        await app.bot.send_message(uid, msg)
    except Exception as e:
        log.error("Nudge error: %s", e)

async def _send_evening(app: Application, uid: int) -> None:
    """Evening reflection (scheduled)."""
    try:
        msg = "🌙 Gute Nacht! Journaling: /journal oder weiter mit /sos?"
        await app.bot.send_message(uid, msg)
    except Exception as e:
        log.error("Evening message error: %s", e)

def _schedule_user_jobs(app: Application, user: UserState) -> None:
    """Schedule daily jobs for user (morning, nudge, evening)."""
    if not user:
        return
    jq = cast(JobQueue, app.job_queue)
    tz = ZoneInfo(user.timezone or TIMEZONE)
    wake = _parse_time(user.wake_time or "07:30")

    # Morning
    jq.run_daily(
        lambda c: _send_morning(app, user.user_id),
        time=time(wake.hour, wake.minute, tzinfo=tz),
        name=f"morning_{user.user_id}",
    )
    # Nudge: wake + 6h
    nudge_t = (datetime.combine(date.today(), wake) + timedelta(hours=6)).time()
    jq.run_daily(
        lambda c: _send_nudge(app, user.user_id),
        time=time(nudge_t.hour, nudge_t.minute, tzinfo=tz),
        name=f"nudge_{user.user_id}",
    )
    # Evening
    jq.run_daily(
        lambda c: _send_evening(app, user.user_id),
        time=time(21, 0, tzinfo=tz),
        name=f"evening_{user.user_id}",
    )

HELP_TEXT = """
/sos — Crisis Toolkit (grounding, breathing, urge surfing, call, distract)
/status — Streak, Erspartes, Mood
/savings [€] — Sparziel setzen
/check — Quick Mood Check
/journal — Tagebucheintrag
/undo — Reset rückgängig
/help — Dieser Text
"""

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_TEXT)

# ══════════════════════════════════════════════════════════════════════════
# APP SETUP
# ══════════════════════════════════════════════════════════════════════════

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("sos", "Soforthilfe bei Craving"),
        BotCommand("status", "Streak & Erspartes"),
        BotCommand("savings", "Geld gespart"),
        BotCommand("check", "Quick Check-in"),
        BotCommand("undo", "Reset rückgängig"),
        BotCommand("journal", "Tagebuch"),
        BotCommand("help", "Alle Befehle"),
    ])
    s: Store = app.bot_data["store"]
    users = await asyncio.to_thread(s.all_users)
    for user in users:
        _schedule_user_jobs(app, user)

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["store"] = Store(DATA_DIR)
    app.bot_data["ai"] = AIClient(
        provider=AI_PROVIDER, model=AI_MODEL,
        openai_key=OPENAI_KEY, anthropic_key=ANTHROPIC_KEY,
    )
    app.bot_data["history"] = History()

    # Security guard
    app.add_handler(TypeHandler(Update, _guard), group=-1)

    # Onboarding conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            1: [MessageHandler(filters.TEXT, on_name)],
        },
        fallbacks=[],
    )
    app.add_handler(conv)

    # Streak commands
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset_streak))
    app.add_handler(CommandHandler("undo", undo_reset))

    # Crisis toolkit /sos
    sos_conv = ConversationHandler(
        entry_points=[CommandHandler("sos", sos)],
        states={
            C_MENU: [CallbackQueryHandler(sos_menu)],
            C_G1: [MessageHandler(filters.TEXT, g1)],
            C_G2: [MessageHandler(filters.TEXT, g2)],
            C_G3: [MessageHandler(filters.TEXT, g3)],
            C_G4: [MessageHandler(filters.TEXT, g4)],
            C_G5: [MessageHandler(filters.TEXT, g5)],
            C_SURF1: [MessageHandler(filters.TEXT, s1)],
            C_SURF2: [MessageHandler(filters.TEXT, s2)],
            C_CONTACT: [MessageHandler(filters.TEXT, on_contact)],
        },
        fallbacks=[CommandHandler("sos", sos)],
    )
    app.add_handler(sos_conv)

    # Savings
    app.add_handler(CommandHandler("savings", savings))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\d+"), on_savings_amount))

    # Journaling
    app.add_handler(CommandHandler("journal", journal))

    # Check-in / mood
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(on_mood, pattern=r"^mood:"))

    # Free-text AI coaching
    app.add_handler(MessageHandler(filters.TEXT, on_text))

    # Help
    app.add_handler(CommandHandler("help", help_cmd))

    # Post-init setup (schedule jobs)
    app.post_init = post_init

    log.info("Starting Unhooked Lite bot")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
