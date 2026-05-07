"""
One-shot backfill: for any non-OPEN positions whose exit_price_cents is NULL,
look up the resolved market in the markets table and back-fill exit prices
plus realized P&L into both positions and pnl tables.

Idempotent: safe to run multiple times. Only writes when a settled
last_price is available in markets and the row hasn't been backfilled yet.

Usage:
    docker exec wb2-trader python3 /app/scripts/backfill_settlements.py
    docker exec wb2-trader python3 /app/scripts/backfill_settlements.py --dry-run
"""

from __future__ import annotations
import os
import sys
import argparse
import datetime as dt
import sqlite3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DB_PATH", "/app/data/bot.db"))
    ap.add_argument("--dry-run", action="store_true", help="Print only, no writes")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found at {args.db}")
        return 2

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row

    rows = c.execute("""
        SELECT p.ticker, p.side, p.qty, p.avg_price_cents, p.opened_at,
               m.last_price, m.status as mkt_status
        FROM positions p
        LEFT JOIN markets m ON m.ticker = p.ticker
        WHERE p.status != 'OPEN'
          AND p.exit_price_cents IS NULL
          AND p.qty IS NOT NULL
          AND p.qty > 0
    """).fetchall()

    if not rows:
        print("No positions to backfill.")
        return 0

    total_pnl = 0.0
    backfilled = 0
    skipped = 0

    print(f"{'DRY-RUN: ' if args.dry_run else ''}Backfilling {len(rows)} positions:\n")
    for r in rows:
        ticker = r["ticker"]
        side = r["side"]
        qty = int(r["qty"])
        entry = int(r["avg_price_cents"]) if r["avg_price_cents"] is not None else None
        last = r["last_price"]
        mstatus = (r["mkt_status"] or "").lower()

        if entry is None or last is None:
            print(f"  ⏭  {ticker:40s} skipped (entry={entry} last={last})")
            skipped += 1
            continue

        # last_price in markets is the YES settle price in cents.
        # NO contract pays (100 - YES_settle).
        if side == "no":
            exit_cents = 100 - int(last)
        else:
            exit_cents = int(last)

        # Only treat 0/100 as settled. If it's anything else, the market
        # may not actually be resolved — be safe.
        if exit_cents not in (0, 100):
            # Look at market status to decide.
            if mstatus not in ("settled", "finalized", "resolved"):
                print(f"  ⏭  {ticker:40s} not settled (last={last}c status={mstatus or 'unknown'})")
                skipped += 1
                continue

        pnl = (exit_cents - entry) * qty / 100.0
        marker = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")
        print(f"  {marker} {ticker:40s} {side} x{qty:<3d} {entry}c -> {exit_cents}c  "
              f"P&L: ${pnl:+.2f}")
        total_pnl += pnl
        backfilled += 1

        if args.dry_run:
            continue

        now = dt.datetime.utcnow().isoformat()
        today = dt.date.today().isoformat()
        c.execute("""
            UPDATE positions
            SET exit_price_cents = ?,
                exit_reason      = 'settlement_backfill',
                status           = 'CLOSED'
            WHERE ticker = ?
              AND exit_price_cents IS NULL
        """, (exit_cents, ticker))
        # Also write a pnl row if not already present.
        existing = c.execute(
            "SELECT 1 FROM pnl WHERE ticker=? AND date(closed_at)=date(?) "
            "AND reason IN ('settlement','settlement_backfill') LIMIT 1",
            (ticker, r["opened_at"]),
        ).fetchone()
        if not existing:
            c.execute("""
                INSERT INTO pnl
                    (ticker, side, qty, entry_cents, exit_cents,
                     realized_usd, reason, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, 'settlement_backfill', ?)
            """, (ticker, side, qty, entry, exit_cents, round(pnl, 4), now))

    if not args.dry_run:
        c.commit()

    print()
    print(f"Backfilled: {backfilled}  |  Skipped: {skipped}  |  "
          f"Total P&L: ${total_pnl:+.2f}")
    if args.dry_run:
        print("(dry-run — no writes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
