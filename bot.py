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
if _raw_ids:
    for x in _raw_ids.split(","):
        if x.strip():
            try:
                ALLOWED_IDS.add(int(x.strip()))
            except ValueError:
                log.error("Invalid ID in ALLOWED_TELEGRAM_CHAT_IDS: %s", x)

# ── Helpers ───────────────────────────────────────────────────────────────

def store(ctx: ContextTypes.DEFAULT_TYPE) -> Store:
    return ctx.bot_data["store"]

def ai(ctx: ContextTypes.DEFAULT_TYPE) -> AIClient:
    return ctx.bot_data["ai"]

def hist(ctx: ContextTypes.DEFAULT_TYPE) -> History:
    return ctx.bot_data["history"]

_TG_MSG_LIMIT = 4096  # Telegram max message length
_TG_SPLIT_AT = 4000   # Split threshold (leave room for edge cases)

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

HABITS = [
    ("🌿 Cannabis", "Cannabis"), ("🚬 Spliffs", "Spliffs"),
    ("🎮 Gaming", "Gaming"), ("🔞 Porn", "Porn"),
    ("🛍️ Kaufsucht", "Kaufsucht"), ("💔 Toxische Beziehung", "Toxic"),
    ("✏️ Anderes", None),
]
LAST_USE = [("today", 0), ("yesterday", 1), ("2-3 days", 2), ("week+", 7)]
TRIGGERS = ["Langeweile", "Stress", "Angst", "Einsamkeit", "Sozialer Druck", "Abends", "Morgens"]

S_HABIT, S_HABIT_TXT, S_LAST, S_WAKE, S_SAVINGS, S_TRIG, S_WHY = range(7)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(label, callback_data=f"h:{name or 'other'}")] for label, name in HABITS]
    await update.message.reply_text("Hey. Ich bin dein Unhooked Coach. Bereit, die Kontrolle zurückzuholen?")
    await update.message.reply_text("Was möchtest du verändern?", reply_markup=InlineKeyboardMarkup(kb))
    return S_HABIT

async def on_habit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data:
        return ConversationHandler.END
    await q.answer()
    val = q.data.split(":", 1)[1]
    ud = ctx.user_data
    if val == "other":
        await q.message.reply_text("Was möchtest du verändern? (Schreib es einfach)")
        return S_HABIT_TXT
    ud["habit"] = val
    kb = [[InlineKeyboardButton(f"{k} ({d}d)", callback_data=f"l:{d}")] for k, d in LAST_USE]
    await q.message.reply_text("Wann war dein letzter Konsum?", reply_markup=InlineKeyboardMarkup(kb))
    return S_LAST

async def on_habit_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END
    ud = ctx.user_data
    ud["habit"] = (update.message.text or "Other").strip()[:50]
    kb = [[InlineKeyboardButton(f"{k} ({d}d)", callback_data=f"l:{d}")] for k, d in LAST_USE]
    await update.message.reply_text("Wann war dein letzter Konsum?", reply_markup=InlineKeyboardMarkup(kb))
    return S_LAST

async def on_last_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data:
        return ConversationHandler.END
    await q.answer()
    ud = ctx.user_data
    ud["quit_days"] = int(q.data.split(":")[1])
    await q.message.reply_text("Wann stehst du normalerweise auf? (HH:MM)")
    return S_WAKE

async def on_wake(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    try:
        h, m = text.split(":")
        time(int(h), int(m))
    except ValueError:
        await update.message.reply_text("Bitte gib die Zeit im Format HH:MM an (z.B. 07:30).")
        return S_WAKE
    ud = ctx.user_data
    ud["wake"] = text
    await update.message.reply_text("Wie viel gibst du ungefähr pro Tag dafür aus? (z.B. 15)")
    return S_SAVINGS

async def on_savings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END
    ud = ctx.user_data
    try:
        ud["savings"] = max(0.0, round(float((update.message.text or "0").replace(",", ".")), 2))
    except ValueError:
        await update.message.reply_text("Bitte gib eine Zahl ein, z.B. 15")
        return S_SAVINGS
    ud["sel_trigs"] = []
    await update.message.reply_text("Wann schlagen Cravings am härtesten zu?", reply_markup=_trig_kb(set()))
    return S_TRIG

def _trig_kb(sel: set[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(("✅ " if t in sel else "") + t, callback_data=f"t:{t}")] for t in TRIGGERS]
    rows.append([InlineKeyboardButton("✔️ Fertig", callback_data="t:DONE")])
    return InlineKeyboardMarkup(rows)

async def on_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data:
        return ConversationHandler.END
    await q.answer()
    val = q.data.split(":", 1)[1]
    ud = ctx.user_data
    sel: list[str] = ud.setdefault("sel_trigs", [])
    if val == "DONE":
        await q.message.reply_text("Was ist dein WARUM? Wofür machst du das?")
        return S_WHY
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
    await q.edit_message_reply_markup(reply_markup=_trig_kb(set(sel)))
    return S_TRIG

async def on_why(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return ConversationHandler.END
    ud = ctx.user_data
    why = (update.message.text or "").strip()
    quit_days = ud.get("quit_days", 0)
    quit_date = datetime.now(ZoneInfo(TIMEZONE)).date() - timedelta(days=quit_days)
    user = UserState(
        user_id=update.effective_user.id,
        username=update.effective_user.username or "",
        habit=ud.get("habit", "Other"),
        why=why,
        quit_date=quit_date.isoformat(),
        wake_time=ud.get("wake", "07:30"),
        triggers=ud.get("sel_trigs", []),
        timezone=TIMEZONE,
        savings_per_day=ud.get("savings", 1.5),
    )
    user.calc_streak()
    store(ctx).save(user)
    _schedule_user_jobs(ctx.application, user)
    await update.message.reply_text(
        f"Alles klar. Tag {user.streak_days} startet jetzt.\n\n"
        f"Dein WARUM: {why}\n\n"
        "Ich bin rund um die Uhr für dich da. Schreib mir einfach. 🪝"
    )
    ud.clear()
    return ConversationHandler.END

async def cmd_cancel(update: Update, _) -> int:
    if update.message:
        await update.message.reply_text("Abgebrochen. /start wenn du bereit bist.")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════
# TRACKING  (/status, /reset, /undo, /why, /savings, /check)
# ══════════════════════════════════════════════════════════════════════════

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user = store(ctx).load(update.effective_user.id)
    if not user:
        await update.message.reply_text("Starte zuerst mit /start.")
        return
    user.calc_streak()
    store(ctx).save(user)
    await update.message.reply_text(
        f"🔥 Streak: {user.streak_days} Tag(e)\n"
        f"🏆 Längster: {user.longest_streak} Tag(e)\n"
        f"💶 Gespart: €{user.savings():.2f}\n"
        f"🎯 Rückfälle: {user.relapses}"
    )

async def cmd_why(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user = store(ctx).load(update.effective_user.id)
    if not user or not user.why:
        await update.message.reply_text("Setz dein WARUM in /start.")
        return
    await update.message.reply_text(f"Dein WARUM: {user.why}")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not ctx.args or ctx.args[0].lower() != "confirm":
        await update.message.reply_text("Das setzt deinen Streak zurück. /reset confirm wenn du sicher bist.")
        return
    user = store(ctx).load(update.effective_user.id)
    if not user:
        await update.message.reply_text("Kein Profil. /start zuerst.")
        return
    user.reset_streak()
    store(ctx).save(user)
    await update.message.reply_text("Reset. Tag 1 startet jetzt. Du bist nicht hinten dran — du bist wieder im Kampf.")

async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user = store(ctx).load(update.effective_user.id)
    if not user:
        await update.message.reply_text("Kein Profil. /start zuerst.")
        return
    if not user.last_relapse_reset:
        await update.message.reply_text("Kein Reset zum Rückgängigmachen.")
        return
    if not user.undo_reset():
        await update.message.reply_text("Undo-Fenster abgelaufen (5 Min).")
        return
    store(ctx).save(user)
    await update.message.reply_text(f"Undo erledigt. Wiederhergestellt auf Tag {user.streak_days}.")

async def cmd_savings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user = store(ctx).load(update.effective_user.id)
    if not user:
        await update.message.reply_text("Starte zuerst mit /start.")
        return
    args = ctx.args or []
    if args and args[0] == "set" and len(args) >= 2:
        try:
            user.savings_per_day = max(0, round(float(args[1]), 2))
            store(ctx).save(user)
            await update.message.reply_text(f"Gespeichert: €{user.savings_per_day:.2f}/Tag.")
        except ValueError:
            await update.message.reply_text("Bitte: /savings set 15")
        return
    if args and args[0] == "goal" and len(args) >= 2:
        try:
            user.savings_goal = max(0, round(float(args[1]), 2))
            store(ctx).save(user)
            await update.message.reply_text(f"🎯 Sparziel: €{user.savings_goal:.2f}")
        except ValueError:
            await update.message.reply_text("Bitte: /savings goal 500")
        return
    user.calc_streak()
    msg = f"💶 Gespart: €{user.savings():.2f} ({user.streak_days} Tage × €{user.savings_per_day:.2f})"
    if user.savings_goal > 0:
        pct = min(100, user.savings() / user.savings_goal * 100)
        msg += f"\n🎯 Ziel: €{user.savings_goal:.2f} ({pct:.0f}%)"
    await update.message.reply_text(msg)

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick check-in: /check <mood> [craving] [stress] (1-10 each)."""
    if not update.message or not update.effective_user:
        return
    user = store(ctx).load(update.effective_user.id)
    if not user:
        await update.message.reply_text("Starte zuerst mit /start.")
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Nutzung: /check 7 3 2 (Stimmung Craving Stress)")
        return
    vals = []
    for a in args[:3]:
        try:
            vals.append(max(1, min(10, int(a))))
        except ValueError:
            pass
    mood = vals[0] if vals else 5
    craving = vals[1] if len(vals) > 1 else None
    stress = vals[2] if len(vals) > 2 else None
    entry = {"date": datetime.now(ZoneInfo("UTC")).isoformat(), "morning": mood, "craving": craving, "stress": stress}
    user.mood_log.append(entry)
    store(ctx).save(user)
    parts = [f"Stimmung: {mood}/10"]
    if craving is not None:
        parts.append(f"Craving: {craving}/10")
    if stress is not None:
        parts.append(f"Stress: {stress}/10")
    await update.message.reply_text("✅ " + " | ".join(parts))

# ══════════════════════════════════════════════════════════════════════════
# JOURNAL  (/journal)
# ══════════════════════════════════════════════════════════════════════════

J_WRITE = 0

async def cmd_journal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return ConversationHandler.END
    user = store(ctx).load(update.effective_user.id)
    if not user:
        await update.message.reply_text("Starte zuerst mit /start.")
        return ConversationHandler.END
    args = ctx.args or []
    if args and args[0] == "list":
        if not user.journal:
            await update.message.reply_text("Noch keine Einträge.")
            return ConversationHandler.END
        lines = [f"#{i+1} {e['date'][:10]}: {e['text'][:60]}" for i, e in enumerate(user.journal[-10:])]
        await update.message.reply_text("\n".join(lines))
        return ConversationHandler.END
    if args and args[0] == "read" and len(args) >= 2:
        recent = user.journal[-10:]
        try:
            idx = int(args[1]) - 1
            e = recent[idx]
            await update.message.reply_text(f"#{idx+1} ({e['date'][:10]}):\n{e['text']}")
        except (IndexError, ValueError):
            await update.message.reply_text(f"Eintrag nicht gefunden. Du hast {len(user.journal)} Einträge.")
        return ConversationHandler.END
    await update.message.reply_text("📓 Schreib einfach drauf los — was beschäftigt dich gerade?\n(/cancel zum Abbrechen)")
    return J_WRITE

async def on_journal_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return ConversationHandler.END
    user = store(ctx).load(update.effective_user.id)
    if not user:
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Schreib einfach deinen Gedanken — oder /cancel.")
        return J_WRITE
    user.journal.append({"date": datetime.now(ZoneInfo("UTC")).isoformat(), "text": text})
    store(ctx).save(user)
    await update.message.reply_text("Gespeichert. Starker Move — Bewusstsein schlägt Autopilot. 📝")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════
# CRISIS TOOLKIT  (/sos, /craving)
# ══════════════════════════════════════════════════════════════════════════

C_MENU, C_G1, C_G2, C_G3, C_G4, C_G5, C_SURF1, C_SURF_R1, C_SURF_R2, C_CONTACT = range(10)

def _sos_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧘 Grounding (5-4-3-2-1)", callback_data="sos:ground")],
        [InlineKeyboardButton("🫁 Atemübung", callback_data="sos:breathe")],
        [InlineKeyboardButton("🌊 Urge Surfing", callback_data="sos:surf")],
        [InlineKeyboardButton("📞 Jemanden anrufen", callback_data="sos:call")],
        [InlineKeyboardButton("🎯 Ablenkung", callback_data="sos:distract")],
    ])

def _fb_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Ja 💚", callback_data="sos:yes"),
        InlineKeyboardButton("Nein", callback_data="sos:no"),
    ]])

def _rating_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(n), callback_data=f"sos:{prefix}_{n}") for n in range(1, 6)],
        [InlineKeyboardButton(str(n), callback_data=f"sos:{prefix}_{n}") for n in range(6, 11)],
    ])

async def cmd_sos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END
    await update.message.reply_text("Ich bin da. Cravings gehen vorbei — lass uns das zusammen durchstehen.")
    await update.message.reply_text("Wähle ein Werkzeug:", reply_markup=_sos_kb())
    return C_MENU

async def sos_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data or not isinstance(q.message, Message):
        return ConversationHandler.END
    await q.answer()
    action = q.data.split(":")[1]
    msg = q.message

    # Cancel any running breathing task on any SOS interaction
    ud = ctx.user_data
    old_task = ud.get("breathing_task")
    if old_task and not old_task.done():
        old_task.cancel()

    if action == "ground":
        await msg.reply_text("🧘 Grounding (5-4-3-2-1)\n\nNenne mir 5 Dinge, die du gerade SIEHST:")
        return C_G1
    if action == "breathe":
        ud["breathing_task"] = asyncio.create_task(_run_breathing(msg))
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
            contact = (ctx.user_data).get("emergency_contact")
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
        tz = ZoneInfo(TIMEZONE)
        h = datetime.now(tz).hour
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
async def surf1(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not isinstance(update.effective_message, Message):
        return ConversationHandler.END
    ud = ctx.user_data
    ud["surf_loc"] = update.effective_message.text
    await update.effective_message.reply_text(
        "Wie stark ist das Craving? (1-10)", reply_markup=_rating_kb("r1")
    )
    return C_SURF_R1

async def surf_r1(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data or not isinstance(q.message, Message):
        return ConversationHandler.END
    await q.answer()
    try:
        r = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        return ConversationHandler.END
    ud = ctx.user_data
    ud["surf_r1"] = r
    m = await q.message.reply_text(f"Intensität: {r}/10\n\n🌊 Beobachte die Welle... ⏳ 2 Min")
    # The try/except around edit_text handles cases where the user
    # cancelled or the message was deleted during the 2-min wait.
    for sec in [90, 60, 30, 0]:
        await asyncio.sleep(30)
        try:
            await m.edit_text(f"🌊 Beobachte... ⏳ {sec}s" if sec else "🌊 Die 2 Minuten sind um.")
        except Exception:
            pass
    await q.message.reply_text("Wie stark ist das Craving JETZT?", reply_markup=_rating_kb("r2"))
    return C_SURF_R2

async def surf_r2(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q or not q.data or not isinstance(q.message, Message):
        return ConversationHandler.END
    await q.answer()
    try:
        r2 = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        return ConversationHandler.END
    r1 = (ctx.user_data).get("surf_r1", r2)
    diff = r1 - r2
    if diff > 0:
        txt = f"Vorher: {r1}/10 → Jetzt: {r2}/10\n\n📉 {diff} Punkte weniger. Die Welle geht vorbei."
    elif diff == 0:
        txt = "Gleich geblieben — das ist okay. Wellen brauchen manchmal länger."
    else:
        txt = "Stärker geworden. Probier noch ein anderes Werkzeug."
    await q.message.reply_text(txt)
    await q.message.reply_text("Hat das geholfen?", reply_markup=_fb_kb())
    return C_MENU

async def on_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not isinstance(update.effective_message, Message):
        return ConversationHandler.END
    contact = (update.effective_message.text or "").strip()
    if not contact:
        await update.effective_message.reply_text("Bitte schick mir einen Namen oder Nummer:")
        return C_CONTACT
    ud = ctx.user_data
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
        kind, data = intent
        if kind == "relapse":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Ja, leider", callback_data="rel:yes")],
                [InlineKeyboardButton("Nein, Missverständnis", callback_data="rel:no")],
            ])
            await update.message.reply_text("Hast du wirklich konsumiert?", reply_markup=kb)
            return
        if kind == "journal":
            user.journal.append({"date": datetime.now(ZoneInfo("UTC")).isoformat(), "text": data})
            store(ctx).save(user)
            await update.message.reply_text("Gespeichert. 📝")
            return

    # AI coaching
    h = hist(ctx)
    # Restore history from persistent storage on first access
    if not h.recent(uid, 1) and user.chat_history:
        for entry in user.chat_history:
            h.add(uid, entry.get("role", "user"), entry.get("text", ""))
    history_text = h.format(uid, 6)
    h.add(uid, "user", text)
    reply = await ai(ctx).reply(user, text, history_text=history_text)
    h.add(uid, "coach", reply)

    # Send (handle Telegram message length limit, split at newline boundaries)
    if len(reply) <= _TG_MSG_LIMIT:
        await update.message.reply_text(reply)
    else:
        while reply:
            if len(reply) <= _TG_SPLIT_AT:
                await update.message.reply_text(reply)
                break
            split_at = reply.rfind("\n", 0, _TG_SPLIT_AT)
            if split_at == -1:
                split_at = _TG_SPLIT_AT
            await update.message.reply_text(reply[:split_at])
            reply = reply[split_at:].lstrip("\n")

    # Persist recent history to user data for restart resilience
    user.chat_history = [{"role": t.role, "text": t.text} for t in h.recent(uid, 20)]
    store(ctx).save(user)

async def on_relapse_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not update.effective_user:
        return
    await q.answer()
    user = store(ctx).load(update.effective_user.id)
    if not user or not q.message:
        return
    if q.data == "rel:yes":
        user.reset_streak()
        store(ctx).save(user)
        await q.message.reply_text("Danke für deine Ehrlichkeit. Tag 1. /undo innerhalb 5 Min wenn Fehler.")
    else:
        await q.message.reply_text("Danke fürs Klarstellen. Dein Streak bleibt. Du machst das gut.")

# ══════════════════════════════════════════════════════════════════════════
# PROACTIVE MESSAGES (morning, nudge, evening)
# ══════════════════════════════════════════════════════════════════════════

async def _proactive(app: Application, uid: int, prompt: str) -> None:
    s: Store = app.bot_data["store"]
    user = s.load(uid)
    if not user:
        return
    client: AIClient = app.bot_data["ai"]
    if not client.configured():
        return
    try:
        reply = await client.reply(user, prompt)
        await app.bot.send_message(chat_id=uid, text=reply)
    except Exception as exc:
        log.error("Proactive msg failed for %d: %s", uid, exc)

async def send_morning(app: Application, uid: int) -> None:
    s: Store = app.bot_data["store"]
    user = s.load(uid)
    if not user:
        return
    user.calc_streak()
    await _proactive(app, uid, (
        f"Schreibe eine persönliche Morgengrußnachricht. "
        f"Tag {user.streak_days} clean, {user.savings():.2f}€ gespart. "
        f"Gewohnheit: {user.habit}. 2-3 Sätze auf Deutsch. Warm und konkret."
    ))

async def send_nudge(app: Application, uid: int) -> None:
    s: Store = app.bot_data["store"]
    user = s.load(uid)
    if not user:
        return
    user.calc_streak()
    await _proactive(app, uid, (
        f"Schreibe einen kurzen Mittags-Nudge. Tag {user.streak_days} clean. "
        f"GENAU ein Satz auf Deutsch. Persönlich, kein Klischee."
    ))

async def send_evening(app: Application, uid: int) -> None:
    s: Store = app.bot_data["store"]
    user = s.load(uid)
    if not user:
        return
    user.calc_streak()
    await _proactive(app, uid, (
        f"Schreibe eine Abendreflexion. Tag {user.streak_days} clean, {user.savings():.2f}€ gespart. "
        f"Gewohnheit: {user.habit}. Tagesrückblick, Anerkennung. 2-3 Sätze Deutsch."
    ))

def _schedule_user_jobs(app: Application, user: UserState) -> None:
    try:
        tz = ZoneInfo(user.timezone or TIMEZONE)
    except Exception:
        log.warning("Invalid timezone '%s' for user %d, falling back to %s", user.timezone, user.user_id, TIMEZONE)
        tz = ZoneInfo(TIMEZONE)
    wake = _parse_time(user.wake_time)
    jq = cast(JobQueue, app.job_queue)
    uid = user.user_id

    # Remove old jobs for this user
    for name in [f"m_{uid}", f"n_{uid}", f"e_{uid}"]:
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()

    async def _job_morning(_ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send_morning(app, uid)

    async def _job_nudge(_ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send_nudge(app, uid)

    async def _job_evening(_ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send_evening(app, uid)

    jq.run_daily(_job_morning, time=time(wake.hour, wake.minute, tzinfo=tz), name=f"m_{uid}")
    nudge_dt = (datetime.combine(datetime.now(tz).date(), wake) + timedelta(hours=6)).time()
    jq.run_daily(_job_nudge, time=time(nudge_dt.hour, nudge_dt.minute, tzinfo=tz), name=f"n_{uid}")
    jq.run_daily(_job_evening, time=time(21, 0, tzinfo=tz), name=f"e_{uid}")

# ══════════════════════════════════════════════════════════════════════════
# HELP
# ══════════════════════════════════════════════════════════════════════════

HELP_TEXT = (
    "Ich bin dein Unhooked Coach. Schreib mir jederzeit.\n\n"
    "/status — Streak, Erspartes\n"
    "/savings — Geld gespart / Ziel setzen\n"
    "/undo — Reset rückgängig (5 Min)\n"
    "/check 7 3 2 — Stimmung/Craving/Stress\n"
    "/sos — Crisis Toolkit\n"
    "/journal — Tagebuch\n"
    "/settings — (coming soon)\n"
    "/help — Diese Hilfe\n\n"
    "Oder schreib einfach frei — ich bin dein Coach."
)

async def cmd_help(update: Update, _) -> None:
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
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            S_HABIT: [CallbackQueryHandler(on_habit, pattern=r"^h:")],
            S_HABIT_TXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_habit_text)],
            S_LAST: [CallbackQueryHandler(on_last_use, pattern=r"^l:")],
            S_WAKE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_wake)],
            S_SAVINGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_savings)],
            S_TRIG: [CallbackQueryHandler(on_trigger, pattern=r"^t:")],
            S_WHY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_why)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    ))

    # Journal conversation
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("journal", cmd_journal)],
        states={J_WRITE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_journal_text)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    ))

    # Crisis toolkit conversation
    txt_filter = filters.TEXT & ~filters.COMMAND
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("sos", cmd_sos), CommandHandler("craving", cmd_sos)],
        states={
            C_MENU: [CallbackQueryHandler(sos_menu, pattern=r"^sos:")],
            C_G1: [MessageHandler(txt_filter, g1)],
            C_G2: [MessageHandler(txt_filter, g2)],
            C_G3: [MessageHandler(txt_filter, g3)],
            C_G4: [MessageHandler(txt_filter, g4)],
            C_G5: [MessageHandler(txt_filter, g5)],
            C_SURF1: [MessageHandler(txt_filter, surf1)],
            C_SURF_R1: [CallbackQueryHandler(surf_r1, pattern=r"^sos:r1_")],
            C_SURF_R2: [CallbackQueryHandler(surf_r2, pattern=r"^sos:r2_")],
            C_CONTACT: [MessageHandler(txt_filter, on_contact)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), CommandHandler("sos", cmd_sos)],
        allow_reentry=True,
    ))

    # Simple commands
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("streak", cmd_status))
    app.add_handler(CommandHandler("why", cmd_why))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("savings", cmd_savings))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("help", cmd_help))

    # Relapse confirmation callback
    app.add_handler(CallbackQueryHandler(on_relapse_cb, pattern=r"^rel:"))

    # Free-text AI coaching (catch-all — must be last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.post_init = post_init
    log.info("Starting Unhooked Lite")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
 
