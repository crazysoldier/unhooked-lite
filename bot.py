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
            log.error("Invalid ID in ALLOWED_TELEGRAM_CHAT_IDS: %s", x)

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
    kb = [[InlineKeyboardButton(label, callback_data=f"h:{name or 'other'}") for label, name in HABITS]]
    await update.message.reply_text("Hey. Ich bin dein Unhooked Coach. Bereit, die Kontrolle zurückzuholen?")
    await update.message.reply_text("Was möchtest du verändern?", reply_markup=InlineKeyboardMarkup(kb))
    return S_HABIT