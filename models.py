"""User model + JSON persistence — single module replaces models/, storage/."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


@dataclass
class UserState:
    user_id: int
    username: str = ""
    habit: str = ""
    why: str = ""
    quit_date: str = field(default_factory=lambda: datetime.now(ZoneInfo("UTC")).date().isoformat())
    wake_time: str = "07:30"
    timezone: str = "Europe/Vienna"
    triggers: list[str] = field(default_factory=list)
    streak_days: int = 0
    longest_streak: int = 0
    relapses: int = 0
    savings_per_day: float = 1.5
    savings_goal: float = 0.0
    mood_log: list[dict] = field(default_factory=list)
    journal: list[dict] = field(default_factory=list)
    chat_history: list[dict] = field(default_factory=list)
    emergency_contact: str = ""
    last_relapse_reset: dict | None = None
    created_at: str = field(default_factory=lambda: datetime.now(ZoneInfo("UTC")).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> UserState:
        # Drop unknown keys so old data files don't crash
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid})

    def _today(self) -> date:
        """Return today's date in the user's configured timezone."""
        return datetime.now(ZoneInfo(self.timezone)).date()

    def calc_streak(self, today: date | None = None) -> int:
        base = today or self._today()
        try:
            quit = date.fromisoformat(self.quit_date)
        except ValueError:
            logger.warning("Invalid quit_date '%s' for user %d, resetting to today", self.quit_date, self.user_id)
            self.quit_date = base.isoformat()
            quit = base
        delta = (base - quit).days + 1
        self.streak_days = max(0, delta)
        self.longest_streak = max(self.longest_streak, self.streak_days)
        return self.streak_days

    def reset_streak(self, reset_date: date | None = None) -> None:
        self.last_relapse_reset = {
            "quit_date": self.quit_date,
            "streak_days": self.streak_days,
            "longest_streak": self.longest_streak,
            "relapses": self.relapses,
            "reset_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }
        self.relapses += 1
        self.quit_date = (reset_date or self._today()).isoformat()
        self.streak_days = 1

    def undo_reset(self, window_s: int = 300) -> bool:
        if not self.last_relapse_reset:
            return False
        try:
            reset_at = datetime.fromisoformat(str(self.last_relapse_reset["reset_at"]))
        except (KeyError, ValueError):
            return False
        if datetime.now(ZoneInfo("UTC")) - reset_at > timedelta(seconds=window_s):
            return False
        self.quit_date = str(self.last_relapse_reset["quit_date"])
        self.streak_days = int(self.last_relapse_reset["streak_days"])
        self.longest_streak = int(self.last_relapse_reset["longest_streak"])
        self.relapses = int(self.last_relapse_reset["relapses"])
        self.last_relapse_reset = None
        return True

    def savings(self) -> float:
        return round(self.savings_per_day * max(self.streak_days, 0), 2)


class Store:
    """Simple JSON file store — one file per user."""

    def __init__(self, data_dir: str = "./data") -> None:
        self.base = Path(data_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, uid: int) -> Path:
        return self.base / f"{uid}.json"

    def load(self, uid: int) -> UserState | None:
        p = self._path(uid)
        if not p.exists():
            return None
        try:
            return UserState.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error("Corrupted data file %s: %s", p, exc)
            return None

    def save(self, user: UserState) -> None:
        path = self._path(user.user_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(user.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def all_users(self) -> list[UserState]:
        users: list[UserState] = []
        for f in self.base.glob("*.json"):
            try:
                users.append(UserState.from_dict(json.loads(f.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.error("Skipping corrupted data file %s: %s", f, exc)
        return users
