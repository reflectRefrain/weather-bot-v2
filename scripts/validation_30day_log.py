#!/usr/bin/env python3
"""
30-day validation log for the morning-window mid_band + lockin_yes deployment.

Snapshots daily into a CSV so we have clean out-of-sample data when the
30-day window closes:
  - per book_type: trades, fills, wins, losses, gross P&L, fee-net P&L
  - per local_hour bucket
  - per city
  - daily kill-switch trips
  - cumulative win rate vs backtest expectation (mid_band 56%, lockin 87%)

Run from the VPS via cron once a day (e.g. 23:55 local) or invoke ad-hoc:
    python3 scripts/validation_30day_log.py
Output: /app/logs/validation_30day.csv (append-only)
"""
import csv
import datetime as dt
import os
import sqlite3
from pathlib import Path

DB_PATH  = os.environ.get("WB2_DB",  "/app/db/wb2.db")
LOG_PATH = os.environ.get("WB2_VAL_LOG", "/app/logs/validation_30day.csv")
START    = dt.date(2026, 5, 8)  # morning-window deployment date

FIELDS = [
    "as_of",
    "day_index",
    "trades_today_total",
    "trades_today_mid_band",
    "trades_today_lockin",
    "wins_today_mid_band",
    "wins_today_lockin",
    "wr_today_mid_band",
    "wr_today_lockin",
    "pnl_today_usd",
    "pnl_cumulative_usd",
    "free_cash_usd",
    "kill_switch_state",
    "open_positions",
    "wr_lifetime_mid_band",
    "wr_lifetime_lockin",
    "trades_lifetime_mid_band",
    "trades_lifetime_lockin",
    "expected_wr_mid_band_56pct",
    "expected_wr_lockin_87pct",
    "delta_mid_band_pp",
    "delta_lockin_pp",
]


def fetch_one(c, sql, *args):
    r = c.execute(sql, args).fetchone()
    return r[0] if r else None


def main():
    today = dt.date.today()
    day_idx = (today - START).days

    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    new_file = not Path(LOG_PATH).exists()

    if not Path(DB_PATH).exists():
        print(f"DB not found at {DB_PATH}; skipping snapshot.")
        return

    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row

    today_iso = today.isoformat()

    def trades_count(book_type, since_iso):
        return fetch_one(
            c,
            """SELECT COUNT(*) FROM positions
               WHERE book_type = ? AND substr(opened_at, 1, 10) >= ?""",
            book_type, since_iso,
        ) or 0

    def wins_count(book_type, since_iso):
        return fetch_one(
            c,
            """SELECT COUNT(*) FROM positions
               WHERE book_type = ? AND status = 'CLOSED'
                 AND substr(closed_at, 1, 10) >= ?
                 AND realized_pnl_cents > 0""",
            book_type, since_iso,
        ) or 0

    def lifetime_trades(book_type):
        return fetch_one(
            c,
            "SELECT COUNT(*) FROM positions WHERE book_type = ? AND status = 'CLOSED'",
            book_type,
        ) or 0

    def lifetime_wins(book_type):
        return fetch_one(
            c,
            """SELECT COUNT(*) FROM positions WHERE book_type = ? AND status = 'CLOSED'
               AND realized_pnl_cents > 0""",
            book_type,
        ) or 0

    def pnl_today_cents():
        return fetch_one(
            c,
            """SELECT COALESCE(SUM(realized_pnl_cents), 0) FROM positions
               WHERE status = 'CLOSED' AND substr(closed_at, 1, 10) = ?""",
            today_iso,
        ) or 0

    def pnl_cumulative_cents():
        return fetch_one(
            c,
            """SELECT COALESCE(SUM(realized_pnl_cents), 0) FROM positions
               WHERE status = 'CLOSED' AND substr(closed_at, 1, 10) >= ?""",
            START.isoformat(),
        ) or 0

    def open_positions_count():
        return fetch_one(
            c, "SELECT COUNT(*) FROM positions WHERE status = 'OPEN'"
        ) or 0

    def state(key):
        v = fetch_one(c, "SELECT value FROM state WHERE key = ?", key)
        return v or ""

    today_mid   = trades_count("mid_band", today_iso)
    today_lock  = trades_count("lockin",   today_iso)
    today_wins_mid  = wins_count("mid_band", today_iso)
    today_wins_lock = wins_count("lockin",   today_iso)
    life_mid_t = lifetime_trades("mid_band")
    life_lock_t = lifetime_trades("lockin")
    life_mid_w = lifetime_wins("mid_band")
    life_lock_w = lifetime_wins("lockin")

    def safe_pct(num, den):
        return round(100 * num / den, 1) if den else None

    wr_mid_today = safe_pct(today_wins_mid, today_mid)
    wr_lock_today = safe_pct(today_wins_lock, today_lock)
    wr_mid_life = safe_pct(life_mid_w, life_mid_t)
    wr_lock_life = safe_pct(life_lock_w, life_lock_t)

    row = {
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "day_index": day_idx,
        "trades_today_total": today_mid + today_lock,
        "trades_today_mid_band": today_mid,
        "trades_today_lockin": today_lock,
        "wins_today_mid_band": today_wins_mid,
        "wins_today_lockin": today_wins_lock,
        "wr_today_mid_band": wr_mid_today,
        "wr_today_lockin": wr_lock_today,
        "pnl_today_usd": round(pnl_today_cents() / 100.0, 2),
        "pnl_cumulative_usd": round(pnl_cumulative_cents() / 100.0, 2),
        "free_cash_usd": "",  # filled by external balance sync if available
        "kill_switch_state": state("kill_switch") or "OFF",
        "open_positions": open_positions_count(),
        "wr_lifetime_mid_band": wr_mid_life,
        "wr_lifetime_lockin": wr_lock_life,
        "trades_lifetime_mid_band": life_mid_t,
        "trades_lifetime_lockin": life_lock_t,
        "expected_wr_mid_band_56pct": 56.0,
        "expected_wr_lockin_87pct": 87.0,
        "delta_mid_band_pp": (round(wr_mid_life - 56.0, 1) if wr_mid_life is not None else None),
        "delta_lockin_pp": (round(wr_lock_life - 87.0, 1) if wr_lock_life is not None else None),
    }

    with open(LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)

    # Stdout summary for cron mail / docker logs
    print(f"[validation day {day_idx}] mid_band trades={today_mid} wins={today_wins_mid} "
          f"WR={wr_mid_today}%  lockin trades={today_lock} wins={today_wins_lock} WR={wr_lock_today}%  "
          f"pnl_today=${row['pnl_today_usd']:.2f}  cumul=${row['pnl_cumulative_usd']:.2f}  "
          f"kill={row['kill_switch_state']}")


if __name__ == "__main__":
    main()
