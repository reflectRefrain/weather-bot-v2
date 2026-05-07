"""
Filter diagnostic — for each lockin/tail_short filter constraint, compute
what fraction of recent decisions met it. Helps decide whether the named
books are 'correctly cautious' or 'structurally too tight' for the way
Kalshi lists strikes and prices.

Usage (host with bind mount, or inside container):
    docker exec wb2-trader python3 /app/scripts/filter_diagnostic.py
    docker exec wb2-trader python3 /app/scripts/filter_diagnostic.py --hours 168
"""

from __future__ import annotations
import os
import sys
import argparse
import sqlite3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24,
                    help="Look-back window in hours (default 24)")
    ap.add_argument("--db", default=os.getenv("DB_PATH", "/app/data/bot.db"))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found at {args.db}")
        return 2

    c = sqlite3.connect(args.db)

    # Use 'strike_low' as the bucket strike (works for 'between' or 'greater').
    # For 'less' contracts the upper bound is the meaningful strike — but those
    # are rare in this product, so close enough for a coverage estimate.
    base_filter = (
        f"ts > datetime('now', '-{args.hours} hours') "
        "AND effective_forecast IS NOT NULL "
        "AND yes_ask IS NOT NULL "
        "AND model_prob_yes IS NOT NULL"
    )

    total = c.execute(f"SELECT COUNT(*) FROM model_decisions WHERE {base_filter}").fetchone()[0]
    if total == 0:
        print(f"No decisions in last {args.hours}h with full model fields.")
        return 0

    print(f"=== Filter diagnostic — last {args.hours}h, n={total} ===\n")

    def pct(where: str, label: str) -> None:
        n = c.execute(
            f"SELECT COUNT(*) FROM model_decisions WHERE {base_filter} AND ({where})"
        ).fetchone()[0]
        bar = "#" * int((n / total) * 40)
        print(f"  {label:50s} {n:5d} / {total:5d}  {n/total*100:5.1f}%  {bar}")

    print("LOCK-IN filters (need ALL three):")
    pct("model_prob_yes >= 0.85",
        "  model_prob_yes >= 0.85")
    pct("yes_ask <= 92",
        "  yes_ask <= 92¢")
    pct("ABS(strike_low - effective_forecast) <= 2 OR "
        "ABS(strike_high - effective_forecast) <= 2",
        "  |strike - forecast| <= 2°F")
    pct("model_prob_yes >= 0.85 AND yes_ask <= 92 AND "
        "(ABS(strike_low - effective_forecast) <= 2 OR "
        "ABS(strike_high - effective_forecast) <= 2)",
        "  ALL THREE (lock-in qualifies)")

    print()
    print("TAIL-SHORT filters (need ALL three):")
    pct("(1.0 - model_prob_yes) >= 0.95",
        "  model_prob_no >= 0.95")
    pct("(100 - yes_bid) BETWEEN 15 AND 30",
        "  no_price in [15¢, 30¢]")
    pct("ABS(strike_low - effective_forecast) >= 4 AND "
        "ABS(strike_high - effective_forecast) >= 4",
        "  |strike - forecast| >= 4°F")
    pct("(1.0 - model_prob_yes) >= 0.95 AND "
        "(100 - yes_bid) BETWEEN 15 AND 30 AND "
        "ABS(strike_low - effective_forecast) >= 4 AND "
        "ABS(strike_high - effective_forecast) >= 4",
        "  ALL THREE (tail-short qualifies)")

    print()
    # Strike-distance distribution: are Kalshi strikes structurally far from forecasts?
    print("Strike-to-forecast distance distribution (lockin needs <= 2°F):")
    rows = c.execute(f"""
        SELECT
            SUM(CASE WHEN ABS(strike_low - effective_forecast) <= 1 THEN 1 ELSE 0 END) le1,
            SUM(CASE WHEN ABS(strike_low - effective_forecast) <= 2 THEN 1 ELSE 0 END) le2,
            SUM(CASE WHEN ABS(strike_low - effective_forecast) <= 3 THEN 1 ELSE 0 END) le3,
            SUM(CASE WHEN ABS(strike_low - effective_forecast) <= 5 THEN 1 ELSE 0 END) le5,
            SUM(CASE WHEN ABS(strike_low - effective_forecast) > 5 THEN 1 ELSE 0 END) gt5
        FROM model_decisions WHERE {base_filter}
    """).fetchone()
    print(f"  <=1°F: {rows[0]}   <=2°F: {rows[1]}   <=3°F: {rows[2]}   "
          f"<=5°F: {rows[3]}   >5°F: {rows[4]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
