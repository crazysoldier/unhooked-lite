# Unhooked Lite

**Simplified Telegram addiction recovery coach bot** — AI-powered support for staying clean, tracking progress, and breaking free from addictive habits.

Reverse-engineered from [unhooked-bot](https://github.com/crazysoldier/unhooked-bot) with **~80% code reduction** (1,085 lines vs 5,000+) while preserving all core features.

## Features

- **Onboarding** (/start) — Choose habit, set quit date, capture your "why"
- **Streak Tracking** (/status) — Days clean, longest streak, relapses, money saved
- **Crisis Toolkit** (/sos) — 5 evidence-based techniques for when cravings hit:
  - 5-4-3-2-1 Grounding exercise
  - 4-2-6 Breathing technique
  - Urge Surfing with intensity tracking
  - Emergency contact system
  - Context-based distraction suggestions
- **AI Coaching** (free-text messages) — OpenAI or Anthropic powered responses tailored to user state
- **Journaling** (/journal) — Log thoughts, list/read past entries
- **Savings Tracking** (/savings) — Calculate money saved, set financial goals
- **Mood Check-ins** (/check) — Quick 1-10 mood, craving, stress tracking
- **Proactive Messages** — Scheduled morning greetings, midday nudges, evening reflections
- **Relapse Detection** — Intent recognition for relapse reports with undo window (5 min)
- **Security** — Optional chat whitelist for private beta access

## Setup

### 1. Environment Variables
Copy and configure:
```bash
cp .env.example .env
```

Required:
- `BOT_TOKEN` — Telegram Bot API token from @BotFather
- `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` — API key for AI provider

Optional:
- `AI_PROVIDER` — "openai" (default) or "anthropic"
- `AI_MODEL` — e.g., "gpt-4o" (default), "claude-3-5-sonnet-20241022"
- `TIMEZONE` — IANA timezone for scheduled messages (default: "Europe/Vienna")
- `DATA_DIR` — User data storage location (default: "./data")
- `ALLOWED_TELEGRAM_CHAT_IDS` — Comma-separated IDs for beta access (optional)

### 2. Installation & Run

**Option A: Docker**
```bash
docker build -t unhooked-lite .

# Using OpenAI (default provider)
docker run -e BOT_TOKEN=<your_token> -e OPENAI_API_KEY=<key> unhooked-lite

# Or using Anthropic
docker run -e BOT_TOKEN=<your_token> \
  -e AI_PROVIDER=anthropic -e AI_MODEL=claude-3-5-sonnet-20241022 \
  -e ANTHROPIC_API_KEY=<key> unhooked-lite
```
At least one of `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is required.

**Option B: Local Python**
```bash
pip install -r requirements.txt
python bot.py
```

## File Structure

- **bot.py** (821 lines) — Main entry point with all Telegram handlers:
  - Onboarding conversation flow
  - Streak tracking commands
  - Crisis toolkit with grounding, breathing, urge surfing
  - AI coaching message handler
  - Proactive scheduled message jobs
  - Security guard

- **coach.py** — AI client abstraction:
  - Support for OpenAI and Anthropic
  - Intent detection for relapse/journaling
  - Conversation history management
  - Context-aware responses using user state

- **models.py** — Data structures and persistence:
  - `UserState` — User profile (habit, streak, savings, journal, mood log)
  - `Store` — JSON-based file storage
  - Streak calculation and undo logic

- **Dockerfile** — Multi-stage build with slim Python base
- **requirements.txt** — Dependencies (python-telegram-bot, openai, anthropic, etc.)
- **.env.example** — Configuration template

## How It Works

1. User starts bot with `/start` — onboarding collects habit, quit date, motivation, triggers
2. Daily proactive messages at wake time, noon, and 9 PM
3. Free-text messages routed to AI coach or special handlers (relapse detection, journaling)
4. Crisis toolkit (/sos) guides through evidence-based coping techniques
5. User data (streak, journal, mood log) persisted to JSON files

## Motivation

The original unhooked-bot (5,000+ lines across 40+ files) was powerful but complex. Unhooked Lite distills it to a single-file entry point plus two supporting modules, keeping all features while dramatically improving maintainability, deployment speed, and ease of customization.

## License

Based on [crazysoldier/unhooked-bot](https://github.com/crazysoldier/unhooked-bot)