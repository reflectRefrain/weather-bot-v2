#!/usr/bin/env python3
"""
check_decisions.py — Morning-after sanity check for model_decisions logging.

Run this on the VPS after the bot has been live overnight to confirm:
  1. The model_decisions table is being written to (logging is alive)
  2. Decision distribution looks sane (not stuck on one branch)
  3. Each city is producing rows (no city silently broken)
  4. Probability calibration is in the right ballpark

Usage on VPS (inside the bot container, or with DB_PATH set):
    python scripts/check_decisions.py
    python scripts/check_decisions.py --hours 12       # last 12 hours
    python scripts/check_decisions.py --since 2026-05-05  # since a date

Or from the host:
    docker compose exec bot python scripts/check_decisions.py
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("DB_PATH", "/app/data/bot.db")


def fmt_pct(x):
    if x is None:
        return "  -- "
    return f"{100*x:5.1f}%"


def fmt_num(x, w=6, dec=2):
    if x is None:
        return " " * w
    return f"{x:{w}.{dec}f}"


def section(title):
    print()
    print(f"── {title} " + "─" * max(0, 70 - len(title)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH, help=f"path to bot DB (default: {DB_PATH})")
    ap.add_argument("--hours", type=float, default=24,
                    help="lookback window in hours (default: 24)")
    ap.add_argument("--since", default=None,
                    help="ISO date or datetime; overrides --hours if given")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found at {args.db}", file=sys.stderr)
        print("Hint: set DB_PATH env var or pass --db", file=sys.stderr)
        sys.exit(2)

    if args.since:
        since_iso = args.since
    else:
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    # 0. Sanity — does the table even exist?
    tbl = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model_decisions'"
    ).fetchone()
    if not tbl:
        print("ERROR: model_decisions table does not exist.")
        print("→ The migration didn't run. Restart the bot or check init_db logs.")
        sys.exit(3)

    print(f"db: {args.db}")
    print(f"window: since {since_iso}")

    # 1. Headline counts
    section("Volume")
    total = con.execute(
        "SELECT COUNT(*) FROM model_decisions WHERE ts >= ?", (since_iso,)
    ).fetchone()[0]
    cycles = con.execute(
        "SELECT COUNT(DISTINCT cycle_id) FROM model_decisions "
        "WHERE ts >= ? AND cycle_id IS NOT NULL", (since_iso,)
    ).fetchone()[0]
    distinct_tickers = con.execute(
        "SELECT COUNT(DISTINCT ticker) FROM model_decisions WHERE ts >= ?", (since_iso,)
    ).fetchone()[0]
    first_ts = con.execute(
        "SELECT MIN(ts), MAX(ts) FROM model_decisions WHERE ts >= ?", (since_iso,)
    ).fetchone()
    print(f"  rows logged       : {total}")
    print(f"  distinct tickers  : {distinct_tickers}")
    print(f"  distinct cycles   : {cycles}  (NULL if scan_once doesn't pass cycle_id yet)")
    print(f"  first row         : {first_ts[0]}")
    print(f"  last  row         : {first_ts[1]}")

    if total == 0:
        print("\n⚠  No rows in window. Either the bot hasn't scanned, the time-of-day")
        print("   gate is blocking entries, or logging isn't firing. Check bot logs.")
        return

    # 2. Decision distribution
    section("Decision distribution")
    rows = con.execute(
        "SELECT decision, COUNT(*) AS n FROM model_decisions "
        "WHERE ts >= ? GROUP BY decision ORDER BY n DESC", (since_iso,)
    ).fetchall()
    width = max(len(r["decision"] or "(null)") for r in rows)
    for r in rows:
        d = r["decision"] or "(null)"
        bar = "█" * min(40, int(40 * r["n"] / total))
        print(f"  {d:<{width}}  {r['n']:>5}  {bar}")

    candidates = sum(r["n"] for r in rows if (r["decision"] or "").startswith("candidate"))
    if candidates == 0:
        print("\n⚠  Zero candidate_yes / candidate_no rows. Bot saw markets but found no edge.")
        print("   Normal if scans ran outside 14:30-16:30 local. Worth a look if it ran during.")

    # 3. Per-city volume — catches a city silently misconfigured
    section("Per-city volume")
    rows = con.execute("""
        SELECT
            COALESCE(city,'?') AS city,
            COUNT(*) AS n,
            SUM(CASE WHEN decision LIKE 'candidate_%' THEN 1 ELSE 0 END) AS cands,
            ROUND(AVG(model_prob_yes), 3) AS avg_pyes,
            ROUND(AVG(market_mid),     3) AS avg_mid,
            ROUND(AVG(edge_yes_cents), 2) AS avg_edge_y
        FROM model_decisions
        WHERE ts >= ?
        GROUP BY city
        ORDER BY n DESC
    """, (since_iso,)).fetchall()
    print(f"  {'city':<6} {'rows':>6} {'cands':>6} {'avg p_yes':>11} {'avg mid':>9} {'avg edge_y':>12}")
    for r in rows:
        print(f"  {r['city']:<6} {r['n']:>6} {r['cands']:>6} "
              f"{fmt_pct(r['avg_pyes']):>11} {fmt_pct(r['avg_mid']):>9} "
              f"{fmt_num(r['avg_edge_y'], 6, 2):>12}c")

    # 3b. Tier 2: obs-trajectory projection method distribution.
    # Skip the section entirely if column doesn't exist (older bot).
    md_cols = [r[1] for r in con.execute("PRAGMA table_info(model_decisions)").fetchall()]
    if "projection_method" in md_cols:
        section("Projection method usage (HIGHTEMP same_day)")
        rows = con.execute("""
            SELECT
                COALESCE(projection_method, '(none)') AS method,
                COUNT(*) AS n,
                ROUND(AVG(projected_high_f - forecast_f), 2) AS avg_proj_minus_fc
            FROM model_decisions
            WHERE ts >= ? AND variable = 'HIGHTEMP' AND horizon = 'same_day'
            GROUP BY method
            ORDER BY n DESC
        """, (since_iso,)).fetchall()
        if not rows:
            print("  (no HIGHTEMP same_day rows)")
        else:
            print(f"  {'method':<14} {'n':>5}  avg(proj - fc)")
            for r in rows:
                d = r["avg_proj_minus_fc"]
                d_str = f"{d:+.2f}F" if d is not None else "   --"
                print(f"  {r['method']:<14} {r['n']:>5}  {d_str}")

    # 4. Sigma in use — catches a stuck/zero sigma
    section("Sigma actually used (by horizon)")
    rows = con.execute("""
        SELECT horizon,
               COUNT(*) AS n,
               ROUND(MIN(sigma_used), 3) AS sigma_min,
               ROUND(AVG(sigma_used), 3) AS sigma_avg,
               ROUND(MAX(sigma_used), 3) AS sigma_max
        FROM model_decisions
        WHERE ts >= ? AND sigma_used IS NOT NULL
        GROUP BY horizon
    """, (since_iso,)).fetchall()
    if not rows:
        print("  (no rows with sigma_used)")
    else:
        print(f"  {'horizon':<10} {'n':>5}  {'min':>6}  {'avg':>6}  {'max':>6}")
        for r in rows:
            print(f"  {r['horizon']:<10} {r['n']:>5}  {r['sigma_min']:>6}  "
                  f"{r['sigma_avg']:>6}  {r['sigma_max']:>6}")

    # 5. Top 5 candidates we surfaced
    section("Top 5 candidates by edge")
    rows = con.execute("""
        SELECT ts, ticker, city, decision, model_prob_yes, market_mid,
               edge_yes_cents, edge_no_cents, entry_price_cents
        FROM model_decisions
        WHERE ts >= ? AND decision LIKE 'candidate_%'
        ORDER BY MAX(COALESCE(edge_yes_cents,-999), COALESCE(edge_no_cents,-999)) DESC
        LIMIT 5
    """, (since_iso,)).fetchall()
    if not rows:
        print("  (none in window)")
    else:
        print(f"  {'ticker':<32} {'city':<5} {'side':<14} {'p_yes':>7} {'mid':>7} {'edge':>6} {'entry':>6}")
        for r in rows:
            edge = max(r["edge_yes_cents"] or -999, r["edge_no_cents"] or -999)
            print(f"  {r['ticker']:<32} {r['city']:<5} {r['decision']:<14} "
                  f"{fmt_pct(r['model_prob_yes']):>7} {fmt_pct(r['market_mid']):>7} "
                  f"{fmt_num(edge, 5, 1):>5}c {fmt_num(r['entry_price_cents'], 4, 0):>4}c")

    # 6. Lock-in exits actually fired
    section("Lock-in exits in pnl")
    pnl_tbl = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pnl'"
    ).fetchone()
    if not pnl_tbl:
        print("  (pnl table not present)")
    else:
        cols = [r[1] for r in con.execute("PRAGMA table_info(pnl)").fetchall()]
        if "reason" not in cols:
            print("  (pnl has no 'reason' column — older schema)")
        else:
            r = con.execute(
                "SELECT COUNT(*) FROM pnl WHERE reason='LOCKIN'"
            ).fetchone()[0]
            print(f"  total LOCKIN exits ever: {r}")
            recent = con.execute(
                "SELECT * FROM pnl WHERE reason='LOCKIN' ORDER BY rowid DESC LIMIT 5"
            ).fetchall()
            for row in recent:
                print(f"    {dict(row)}")

    print()
    print("✓ check complete")


if __name__ == "__main__":
    main()
