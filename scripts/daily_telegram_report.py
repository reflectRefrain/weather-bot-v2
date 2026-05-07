"""
Daily decisions report → Telegram.

Runs check_decisions.py, captures the output, and posts a trimmed
summary to the Telegram chat configured via TELEGRAM_TOKEN /
TELEGRAM_CHAT_ID env vars (same vars used by the live bot).

Designed to be invoked once per day from cron, inside the same
container image as the trader so module imports + env are identical.

Usage (host cron):
    docker exec wb2-trader python3 /app/scripts/daily_telegram_report.py

Exit codes:
    0 — sent (or silently skipped if Telegram env not set)
    1 — check_decisions raised
    2 — telegram send raised
"""

from __future__ import annotations

import io
import os
import sys
import datetime as dt
import contextlib
import traceback

# Ensure /app/src is on path (container sets PYTHONPATH=/app/src already,
# but we make this resilient to host invocation too).
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Telegram limit — leave headroom for our header.
TELEGRAM_MAX = 4096
HEADER_BUDGET = 200
BODY_BUDGET = TELEGRAM_MAX - HEADER_BUDGET


def _run_check_decisions() -> str:
    """Import and run check_decisions, capture stdout."""
    import importlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        # check_decisions runs at import time (script-style).
        # Force a clean re-import in case it was already loaded.
        if "check_decisions" in sys.modules:
            del sys.modules["check_decisions"]
        scripts_dir = HERE
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        importlib.import_module("check_decisions")
    return buf.getvalue()


def _trim_for_telegram(text: str, limit: int) -> str:
    """Trim from the top, keeping the most recent/important sections.

    check_decisions prints sections in order: volume, then 3a/3b/3c/3d.
    The most actionable info (NBM coverage, book breakdown) is at the
    bottom, so when we have to cut we drop the *top*.
    """
    if len(text) <= limit:
        return text
    truncated = text[-limit:]
    # Avoid cutting mid-line.
    nl = truncated.find("\n")
    if nl != -1 and nl < 200:
        truncated = truncated[nl + 1:]
    return "…(trimmed)\n" + truncated


def _pnl_summary() -> str:
    """Compute realized P&L from the positions table for the last 24h and 7d.

    Pulls only positions that have exit_price_cents populated (so settlements
    that haven't been reconciled yet are correctly excluded). Splits by
    book_type when that column exists in model_decisions for the same ticker
    so the report shows per-strategy attribution.
    """
    import sqlite3
    db = os.getenv("DB_PATH", "/app/data/bot.db")
    if not os.path.exists(db):
        return ""
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row

    def _pnl_for_window(hours: int) -> tuple[int, int, int, float]:
        rows = c.execute(f"""
            SELECT side, qty, avg_price_cents, exit_price_cents
            FROM positions
            WHERE status != 'OPEN'
              AND exit_price_cents IS NOT NULL
              AND opened_at > datetime('now', '-{hours} hours')
        """).fetchall()
        wins = losses = 0
        net = 0.0
        for r in rows:
            pnl = (r["exit_price_cents"] - r["avg_price_cents"]) * r["qty"] / 100.0
            net += pnl
            if pnl > 0: wins += 1
            elif pnl < 0: losses += 1
        return len(rows), wins, losses, net

    n24, w24, l24, p24 = _pnl_for_window(24)
    n7, w7, l7, p7 = _pnl_for_window(168)
    open_rows = c.execute(
        "SELECT COUNT(*) FROM positions WHERE status='OPEN'"
    ).fetchone()[0]

    out = ["\n── P&L ──"]
    if n24 == 0:
        out.append("  Last 24h: no settled positions")
    else:
        wr = w24 / max(1, w24 + l24) * 100
        out.append(f"  Last 24h: {n24} closed | {w24}W/{l24}L | win {wr:.0f}% | ${p24:+.2f}")
    if n7 != n24:
        wr7 = w7 / max(1, w7 + l7) * 100
        out.append(f"  Last 7d : {n7} closed | {w7}W/{l7}L | win {wr7:.0f}% | ${p7:+.2f}")
    out.append(f"  Open positions: {open_rows}")
    return "\n".join(out) + "\n"


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    header = (
        f"📊 Weather Bot — Daily Report\n"
        f"🕐 {now.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"──────────────────────────────────\n"
    )

    # 1a) P&L summary first (most important — never trim this part).
    try:
        pnl_block = _pnl_summary()
    except Exception:
        pnl_block = "\n(P&L summary failed)\n"

    # 1b) Decision report (NBM coverage, book breakdown, etc.).
    try:
        body = _run_check_decisions()
    except SystemExit as e:  # check_decisions may sys.exit on no DB
        body = f"check_decisions exited (code={e.code}). DB not ready or empty?\n"
    except Exception:
        body = "check_decisions raised:\n" + traceback.format_exc()

    body = pnl_block + body

    body = _trim_for_telegram(body, BODY_BUDGET)
    msg = header + body
    del pnl_block  # keep namespace clean

    # 2) Post to Telegram.
    try:
        from telegram_bot import notify_sync  # noqa: WPS433
    except Exception as e:
        print(f"telegram_bot import failed: {e}", file=sys.stderr)
        # Fall back to stdout so cron still leaves a trail.
        print(msg)
        return 2

    if not os.getenv("TELEGRAM_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        # No telegram configured — print and exit cleanly so cron isn't noisy.
        print(msg)
        return 0

    try:
        notify_sync(msg)
    except Exception:
        print("telegram send failed:\n" + traceback.format_exc(), file=sys.stderr)
        print(msg)  # leave trail
        return 2

    print(f"sent {len(msg)} chars to telegram")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
