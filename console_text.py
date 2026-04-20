"""
console_text.py
---------------
A lightweight developer tool that enhances standard logging by sending
real-time Telegram alerts for critical errors.

Usage:
    from console_text import console

    console.text("Something went wrong!")           # sends alert + prints
    console.text("DB error", level="ERROR")         # with explicit level
    console.log("Starting server...")               # local print only, no alert
"""

import os
import sys
import time
import threading
import traceback
import urllib.request
import urllib.parse
import urllib.error
import json
from datetime import datetime
from collections import deque
from dotenv import load_dotenv

load_dotenv()

# Ensure Windows terminal handles emojis correctly
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ─────────────────────────────────────────────
#  Internal Telegram sender
# ─────────────────────────────────────────────

def _send_telegram(token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API (no third-party deps)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print(f"[console.text] Telegram HTTP error: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        print(f"[console.text] Telegram connection error: {e.reason}")
    except Exception as e:
        print(f"[console.text] Unexpected Telegram error: {e}")
    return False


# ─────────────────────────────────────────────
#  Rate limiter
# ─────────────────────────────────────────────

class _RateLimiter:
    """
    Sliding-window rate limiter.
    Default: max 5 alerts per 60 seconds per unique message key.
    """

    def __init__(self, max_calls: int = 5, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window = window_seconds
        self._buckets: dict[str, deque] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = deque()
            bucket = self._buckets[key]
            # Remove timestamps outside the window
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                return False
            bucket.append(now)
            
            # Memory safety: clean up old entries from other buckets occasionally
            # (only if we have many buckets)
            if len(self._buckets) > 100:
                self._cleanup_all(now)
                
            return True

    def _cleanup_all(self, now: float):
        """Internal helper to remove completely empty or stale buckets."""
        to_delete = []
        for key, bucket in self._buckets.items():
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if not bucket:
                to_delete.append(key)
        for key in to_delete:
            del self._buckets[key]

    def remaining(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key, deque())
            valid = [t for t in bucket if now - t <= self.window]
            return max(0, self.max_calls - len(valid))


# ─────────────────────────────────────────────
#  Alert history store
# ─────────────────────────────────────────────

class _AlertHistory:
    """In-memory store for sent alerts (last 200 entries)."""

    def __init__(self, maxlen: int = 200):
        self._store: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, record: dict):
        with self._lock:
            self._store.append(record)

    def all(self) -> list:
        with self._lock:
            return list(self._store)

    def clear(self):
        with self._lock:
            self._store.clear()

    def summary(self) -> dict:
        records = self.all()
        levels = {}
        for r in records:
            levels[r["level"]] = levels.get(r["level"], 0) + 1
        return {
            "total": len(records),
            "by_level": levels,
            "latest": records[-1] if records else None,
        }


# ─────────────────────────────────────────────
#  Main Console class
# ─────────────────────────────────────────────

LEVEL_EMOJI = {
    "INFO":     "ℹ️",
    "WARNING":  "⚠️",
    "ERROR":    "🔴",
    "CRITICAL": "🚨",
    "DEBUG":    "🐛",
}

LEVEL_COLORS = {          # ANSI codes for terminal output
    "INFO":     "\033[94m",
    "WARNING":  "\033[93m",
    "ERROR":    "\033[91m",
    "CRITICAL": "\033[1;91m",
    "DEBUG":    "\033[90m",
    "RESET":    "\033[0m",
}


class Console:
    """
    Drop-in enhanced logger with Telegram alerting.

    Environment variables (loaded from .env):
        TELEGRAM_BOT_TOKEN   – your bot token from @BotFather
        TELEGRAM_CHAT_ID     – target group/chat ID

    Optional config kwargs (passed to Console()):
        rate_limit_calls     – max alerts per window (default 5)
        rate_limit_window    – window in seconds (default 60)
        app_name             – label shown in Telegram messages
        include_traceback    – auto-append current traceback (default False)
        async_send           – send Telegram in background thread (default True)
    """

    def __init__(
        self,
        rate_limit_calls: int = 5,
        rate_limit_window: int = 60,
        app_name: str = "",
        include_traceback: bool = False,
        async_send: bool = False,
    ):
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._app_name = app_name or os.getenv("APP_NAME", "App")
        self._include_traceback = include_traceback
        self._async = async_send

        self._limiter = _RateLimiter(rate_limit_calls, rate_limit_window)
        self.history = _AlertHistory()

        if not self._token or not self._chat_id:
            print(
                "[console.text] ⚠️  TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
                "Alerts will print locally only."
            )

    # ── Public API ────────────────────────────

    def text(
        self,
        message: str,
        level: str = "ERROR",
        include_traceback: bool | None = None,
        extra: dict | None = None,
    ):
        """
        Send a Telegram alert AND print to console.

        Args:
            message:           The alert message.
            level:             INFO | WARNING | ERROR | CRITICAL | DEBUG
            include_traceback: Override instance-level setting for this call.
            extra:             Optional dict of key/value pairs appended to alert.
        """
        level = level.upper()
        tb = self._resolve_traceback(include_traceback)
        self._print_local(message, level)
        self._dispatch(message, level, traceback_str=tb, extra=extra)

    def log(self, message: str, level: str = "INFO"):
        """Print to console only — no Telegram alert."""
        self._print_local(message, level)

    def info(self, message: str, **kwargs):
        self.text(message, level="INFO", **kwargs)

    def warning(self, message: str, **kwargs):
        self.text(message, level="WARNING", **kwargs)

    def error(self, message: str, **kwargs):
        self.text(message, level="ERROR", **kwargs)

    def critical(self, message: str, **kwargs):
        self.text(message, level="CRITICAL", **kwargs)

    def debug(self, message: str, **kwargs):
        self.text(message, level="DEBUG", **kwargs)

    def dashboard(self):
        """Print a summary dashboard of alert history to the terminal."""
        summary = self.history.summary()
        sep = "─" * 44
        print(f"\n{sep}")
        print("  📊  console.text — Alert Dashboard")
        print(sep)
        print(f"  Total alerts sent : {summary['total']}")
        if summary["by_level"]:
            print("  By level          :")
            for lvl, count in summary["by_level"].items():
                emoji = LEVEL_EMOJI.get(lvl, "•")
                print(f"    {emoji}  {lvl:<10} {count}")
        if summary["latest"]:
            lat = summary["latest"]
            print(f"  Last alert        : [{lat['level']}] {lat['message'][:60]}")
            print(f"  Timestamp         : {lat['timestamp']}")
        print(f"{sep}\n")

    def clear_history(self):
        """Clear in-memory alert history."""
        self.history.clear()
        print("[console.text] Alert history cleared.")

    # ── Internal helpers ──────────────────────

    def _resolve_traceback(self, override: bool | None) -> str:
        use = override if override is not None else self._include_traceback
        if not use:
            return ""
        tb = traceback.format_exc()
        # format_exc returns 'NoneType: None' string if no exception is active
        if tb.strip() in ("NoneType: None", "None"):
            return ""
        return tb

    def _print_local(self, message: str, level: str):
        color = LEVEL_COLORS.get(level, "")
        reset = LEVEL_COLORS["RESET"]
        emoji = LEVEL_EMOJI.get(level, "•")
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"{color}[{ts}] {emoji} {level}: {message}{reset}")

    def _dispatch(self, message: str, level: str, traceback_str: str, extra: dict | None):
        if not self._token or not self._chat_id:
            self._record(message, level, sent=False, reason="no credentials")
            return

        rate_key = f"{level}:{message[:80]}"
        if not self._limiter.is_allowed(rate_key):
            remaining = self._limiter.remaining(rate_key)
            print(
                f"[console.text] 🚫 Rate limit hit for '{message[:40]}...'. "
                f"Remaining budget: {remaining}"
            )
            self._record(message, level, sent=False, reason="rate limited")
            return

        telegram_msg = self._format_message(message, level, traceback_str, extra)

        if self._async:
            t = threading.Thread(
                target=self._send_and_record,
                args=(message, level, telegram_msg),
                daemon=True,
            )
            t.start()
        else:
            self._send_and_record(message, level, telegram_msg)

    def _send_and_record(self, message: str, level: str, telegram_msg: str):
        ok = _send_telegram(self._token, self._chat_id, telegram_msg)
        self._record(message, level, sent=ok, reason="" if ok else "send failed")

    def _format_message(
        self, message: str, level: str, traceback_str: str, extra: dict | None
    ) -> str:
        emoji = LEVEL_EMOJI.get(level, "•")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"{emoji} <b>[{level}]</b> — {self._app_name}",
            f"<code>{self._escape(message)}</code>",
            f"🕐 {ts}",
        ]
        if extra:
            lines.append("")
            for k, v in extra.items():
                lines.append(f"• <b>{k}</b>: {self._escape(str(v))}")
        if traceback_str:
            lines.append("")
            lines.append("<b>Traceback:</b>")
            lines.append(f"<pre>{self._escape(traceback_str[-1000:])}</pre>")
        return "\n".join(lines)

    @staticmethod
    def _escape(text: str) -> str:
        """Escape HTML special chars for Telegram HTML parse mode."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _record(self, message: str, level: str, sent: bool, reason: str):
        self.history.add({
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message,
            "sent": sent,
            "reason": reason,
        })


# ─────────────────────────────────────────────
#  Default singleton — import and use directly
# ─────────────────────────────────────────────

console = Console()
