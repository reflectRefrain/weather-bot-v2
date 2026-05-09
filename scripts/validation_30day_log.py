#!/usr/bin/env python3
"""
30-day validation log for the morning-window mid_band + lockin_yes deployment.

Daily snapshot to a CSV so we have clean out-of-sample data when the 30-day
window closes.

Schema notes (these matter — earlier version of this script had wrong assumptions):
  - DB lives at /app/data/bot.db (not /app/db/wb2.db)
  - The `positions` table has NO `book_type` column. To get book_type per
    fill we join through `model_decisions` on ticker (latest candidate row
    per ticker).
  - The `positions` table has NO `realized_pnl_cents` and NO `closed_at`.
    Realized P&L lives in the `pnl` table as `realized_usd` with `closed_at`.
  - State table key for kill switch is `kill_switch_today` per src/risk.py.
"""
import csv
import datetime as dt
import os
import sqlite3
from pathlib import Path

DB_PATH  = os.environ.get("WB2_DB",  "/app/data/bot.db")
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
    "kill_switch_state",
    "open_positions",
    "wr_lifetime_mid_band",
    "wr_lifetime_lockin",
    "trades_lifetime_mid_band",
    "trades_lifetime_lockin",
    "expected_wr_mid_band",
    "expected_wr_lockin",
    "delta_mid_band_pp",
    "delta_lockin_pp",
]

# Backtest expectations (mid_band 46-48% WR, lockin 75-87% WR per FINDINGS.md).
# Use the conservative-end gates the user committed to as the validation
# threshold: mid_band >=50%, lockin >=75%.
EXPECTED_WR_MID    = 50.0
EXPECTED_WR_LOCKIN = 75.0


def fetch_one(c, sql, *args):
    r = c.execute(sql, args).fetchone()
    return r[0] if r else None


def book_type_for(c, ticker):
    """Latest candidate book_type for a given ticker from model_decisions.
    Returns None if not found."""
    row = c.execute(
        """SELECT book_type FROM model_decisions
           WHERE ticker = ? AND book_type IS NOT NULL
           ORDER BY ts DESC LIMIT 1""",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


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
    start_iso = START.isoformat()

    # All settled trades since deployment date.
    settled_rows = c.execute(
        """SELECT ticker, side, qty, entry_cents, exit_cents,
                  realized_usd, closed_at
           FROM pnl
           WHERE substr(closed_at, 1, 10) >= ?""",
        (start_iso,),
    ).fetchall()

    settled = []
    for r in settled_rows:
        d = dict(r)
        d["book_type"] = book_type_for(c, d["ticker"]) or "unknown"
        settled.append(d)

    def is_today(r):
        return (r["closed_at"] or "")[:10] == today_iso

    def is_win(r):
        return (r["realized_usd"] or 0) > 0

    today_settled = [r for r in settled if is_today(r)]
    today_mid     = [r for r in today_settled if r["book_type"] == "mid_band"]
    today_lock    = [r for r in today_settled if r["book_type"] == "lockin"]

    life_mid  = [r for r in settled if r["book_type"] == "mid_band"]
    life_lock = [r for r in settled if r["book_type"] == "lockin"]

    def wr_pct(rows):
        if not rows:
            return None
        wins = sum(1 for r in rows if is_win(r))
        return round(100 * wins / len(rows), 1)

    pnl_today  = round(sum(r["realized_usd"] or 0 for r in today_settled), 2)
    pnl_cum    = round(sum(r["realized_usd"] or 0 for r in settled), 2)

    # Open positions (current snapshot)
    open_count = fetch_one(c, "SELECT COUNT(*) FROM positions WHERE status = 'OPEN'") or 0

    # Kill switch state — read whatever key exists; fall back to "OFF".
    ks = fetch_one(c, "SELECT value FROM state WHERE key = 'kill_switch_today'") \
         or fetch_one(c, "SELECT value FROM state WHERE key = 'kill_switch'") \
         or "OFF"

    wr_mid_life  = wr_pct(life_mid)
    wr_lock_life = wr_pct(life_lock)

    row = {
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "day_index": day_idx,
        "trades_today_total": len(today_settled),
        "trades_today_mid_band": len(today_mid),
        "trades_today_lockin": len(today_lock),
        "wins_today_mid_band": sum(1 for r in today_mid if is_win(r)),
        "wins_today_lockin": sum(1 for r in today_lock if is_win(r)),
        "wr_today_mid_band": wr_pct(today_mid),
        "wr_today_lockin": wr_pct(today_lock),
        "pnl_today_usd": pnl_today,
        "pnl_cumulative_usd": pnl_cum,
        "kill_switch_state": ks,
        "open_positions": open_count,
        "wr_lifetime_mid_band": wr_mid_life,
        "wr_lifetime_lockin": wr_lock_life,
        "trades_lifetime_mid_band": len(life_mid),
        "trades_lifetime_lockin": len(life_lock),
        "expected_wr_mid_band": EXPECTED_WR_MID,
        "expected_wr_lockin": EXPECTED_WR_LOCKIN,
        "delta_mid_band_pp": (round(wr_mid_life - EXPECTED_WR_MID, 1)
                              if wr_mid_life is not None else None),
        "delta_lockin_pp": (round(wr_lock_life - EXPECTED_WR_LOCKIN, 1)
                            if wr_lock_life is not None else None),
    }

    with open(LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)

    print(f"Snapshot written for {today_iso} (day_index={day_idx}). "
          f"trades_today={len(today_settled)} pnl_today=${pnl_today} "
          f"cum=${pnl_cum} open={open_count} ks={ks}")


if __name__ == "__main__":
    main()
