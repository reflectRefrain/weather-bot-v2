"""
One-shot backfill: for every position whose exit_price_cents is NULL but the
market has actually resolved on Kalshi, write the real settlement values into
both the positions and pnl tables.

Source-of-truth strategy:
  1) Query Kalshi /markets/{ticker} for each stuck position.
     - status == 'finalized' or 'determined'  ->  use result + settlement_value
     - result == 'yes'  ->  YES side pays $1, NO side pays $0
     - result == 'no'   ->  YES side pays $0, NO side pays $1
  2) If the API call fails or the market isn't settled, fall back to the
     local markets.last_price (1c -> NO won, 99c -> YES won, else skip).

Idempotent: only writes when exit_price_cents is still NULL.

Usage:
    docker exec wb2-trader python3 /app/scripts/backfill_settlements.py
    docker exec wb2-trader python3 /app/scripts/backfill_settlements.py --dry-run
    docker exec wb2-trader python3 /app/scripts/backfill_settlements.py --no-api  # skip Kalshi API, use markets table only
"""

from __future__ import annotations
import os
import sys
import argparse
import datetime as dt
import sqlite3
import time

# Ensure src/ is importable when running from /app
sys.path.insert(0, "/app/src")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))


def _resolve_via_api(client, ticker: str) -> tuple[int | None, str]:
    """
    Returns (yes_settle_cents, reason).
      yes_settle_cents: 0 or 100 if settled; None if not yet settled or error.
      reason: human-readable explanation for the log.
    """
    try:
        resp = client.get_market(ticker)
    except Exception as e:
        return None, f"api_error: {str(e)[:80]}"

    m = (resp or {}).get("market") or resp or {}
    status = (m.get("status") or "").lower()
    result = (m.get("result") or "").lower()
    sv = m.get("settlement_value")  # int cents, only present after determination

    if status in ("finalized", "determined", "settled") or result in ("yes", "no"):
        if result == "yes":
            return 100, f"api: status={status} result=yes"
        if result == "no":
            return 0, f"api: status={status} result=no"
        # status says settled but result missing — fall back to settlement_value
        if isinstance(sv, (int, float)):
            sv_int = int(sv)
            if sv_int >= 50:
                return 100, f"api: status={status} sv={sv_int}"
            return 0, f"api: status={status} sv={sv_int}"
        return None, f"api: status={status} but no result/settlement_value"

    return None, f"api: status={status or 'unknown'} (not settled)"


def _resolve_via_local(last_yes_cents: int | None, mkt_status: str) -> tuple[int | None, str]:
    """Heuristic fallback using locally cached markets.last_price."""
    if last_yes_cents is None:
        return None, "local: last_price=None"
    last_yes = int(last_yes_cents)
    if last_yes in (0, 1):
        return 0, f"local: last={last_yes}c (NO won)"
    if last_yes in (99, 100):
        return 100, f"local: last={last_yes}c (YES won)"
    return None, f"local: last={last_yes}c not at extreme (status={mkt_status or 'unknown'})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DB_PATH", "/app/data/bot.db"))
    ap.add_argument("--dry-run", action="store_true", help="Print only, no writes")
    ap.add_argument("--no-api", action="store_true",
                    help="Skip Kalshi API; only use cached markets.last_price")
    ap.add_argument("--rate-sleep-ms", type=int, default=120,
                    help="Sleep between Kalshi API calls (default 120ms)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found at {args.db}")
        return 2

    # Lazy-init Kalshi client only if --no-api is not set.
    client = None
    if not args.no_api:
        try:
            from kalshi_client import KalshiClient  # type: ignore
            client = KalshiClient()
            # Light sanity ping
            _ = client.balance()
            print("Kalshi API: connected")
        except Exception as e:
            print(f"Kalshi API: unavailable ({str(e)[:120]}) — falling back to local data only")
            client = None

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row

    rows = c.execute("""
        SELECT p.ticker, p.side, p.qty, p.avg_price_cents, p.opened_at,
               m.last_price, m.status as mkt_status
        FROM positions p
        LEFT JOIN markets m ON m.ticker = p.ticker
        WHERE p.exit_price_cents IS NULL
          AND p.qty IS NOT NULL
          AND p.qty > 0
    """).fetchall()

    if not rows:
        print("No positions to backfill.")
        return 0

    total_pnl = 0.0
    backfilled = 0
    skipped = 0
    skip_reasons: dict[str, int] = {}

    print(f"{'DRY-RUN: ' if args.dry_run else ''}Examining {len(rows)} stuck positions:\n")

    for r in rows:
        ticker = r["ticker"]
        side = (r["side"] or "").lower()
        qty = int(r["qty"])
        entry = int(r["avg_price_cents"]) if r["avg_price_cents"] is not None else None

        if entry is None:
            print(f"  ⏭  {ticker:40s} skipped (no entry price)")
            skipped += 1
            skip_reasons["no_entry_price"] = skip_reasons.get("no_entry_price", 0) + 1
            continue

        # Try Kalshi API first
        yes_settle = None
        reason = ""
        if client is not None:
            yes_settle, reason = _resolve_via_api(client, ticker)
            time.sleep(args.rate_sleep_ms / 1000.0)

        # Fall back to local cached data
        if yes_settle is None:
            local_settle, local_reason = _resolve_via_local(r["last_price"], r["mkt_status"])
            if local_settle is not None:
                yes_settle = local_settle
                reason = (reason + " | " if reason else "") + local_reason
            else:
                reason = (reason + " | " if reason else "") + local_reason

        if yes_settle is None:
            print(f"  ⏭  {ticker:40s} not settled  ({reason})")
            skipped += 1
            key = reason.split(":", 1)[0].strip()[:30] if reason else "unknown"
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
            continue

        # NO contract pays (100 - YES_settle); YES contract pays YES_settle.
        if side == "no":
            exit_cents = 100 - yes_settle
        else:
            exit_cents = yes_settle

        pnl = (exit_cents - entry) * qty / 100.0
        marker = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")
        print(f"  {marker} {ticker:40s} {side} x{qty:<3d} {entry}c -> {exit_cents}c  "
              f"P&L: ${pnl:+.2f}  ({reason})")
        total_pnl += pnl
        backfilled += 1

        if args.dry_run:
            continue

        now = dt.datetime.utcnow().isoformat()
        c.execute("""
            UPDATE positions
            SET exit_price_cents = ?,
                exit_reason      = 'settlement_backfill',
                status           = 'CLOSED'
            WHERE ticker = ?
              AND exit_price_cents IS NULL
        """, (exit_cents, ticker))

        existing = c.execute(
            "SELECT 1 FROM pnl WHERE ticker=? AND reason IN ('settlement','settlement_backfill') LIMIT 1",
            (ticker,),
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
    if skip_reasons:
        print("Skip reasons:")
        for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"  {v:>3d}  {k}")
    if args.dry_run:
        print("(dry-run — no writes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
