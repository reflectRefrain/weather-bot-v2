"""
MARKET CALIBRATION ANALYSIS

For every (ticker, target_date) we have prices and a known outcome.
Bucket by morning yes_ask (the price you'd pay to buy YES) and ask:
  - What fraction actually resolve YES?
  - Compare to the price (which IS the market's implied probability).

If markets are well-calibrated, win rate ≈ price.
If buyers underpay (positive expected value), win rate > price (after fees).
If buyers overpay (negative expected value), win rate < price (after fees).

This tells us: at what price ranges is the Kalshi KXHIGH market
inefficient enough to give a bot a real edge?
"""

import csv
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
SETTLEMENTS = ROOT / "kalshi_settlements.csv"
PRICES = ROOT / "morning_prices.csv"


def fee_cents(price_cents: float) -> float:
    if price_cents is None or price_cents <= 0 or price_cents >= 100:
        return 0.0
    p = price_cents / 100.0
    return math.ceil(7 * p * (1 - p))


def select_morning_quote(prices_for_ticker):
    snaps = sorted(prices_for_ticker, key=lambda r: int(r.get("ts_end") or 0))
    for s in snaps:
        try:
            yb = s.get("yes_bid_close")
            ya = s.get("yes_ask_close")
            mp = s.get("mean_price")
            if yb in ("", None) or ya in ("", None):
                continue
            yb_c = round(float(yb) * 100)
            ya_c = round(float(ya) * 100)
            mp_c = round(float(mp) * 100) if mp not in ("", None) else None
            if yb_c == 0 and ya_c == 100:
                continue
            return yb_c, ya_c, mp_c
        except Exception:
            continue
    return None, None, None


def main():
    settlements = list(csv.DictReader(open(SETTLEMENTS)))
    settle_by_ticker = {r["ticker"]: r for r in settlements}

    prices_by_ticker = defaultdict(list)
    for r in csv.DictReader(open(PRICES)):
        prices_by_ticker[r["ticker"]].append(r)

    # Build quote+outcome list
    quotes = []
    for tk, mkt in settle_by_ticker.items():
        snaps = prices_by_ticker.get(tk, [])
        yb, ya, mp_c = select_morning_quote(snaps)
        if yb is None: continue
        quotes.append({
            "ticker": tk,
            "city": mkt["city"],
            "strike_type": mkt["strike_type"],
            "yes_bid": yb, "yes_ask": ya, "mean_price": mp_c,
            "result_yes": (mkt["result"] == "yes"),
        })

    print(f"Quotes with outcomes: {len(quotes)}\n")

    # ========== YES-side calibration ==========
    print("="*78)
    print("YES SIDE CALIBRATION  (buy YES @ yes_ask in cents)")
    print("How often YES-buyers WIN at each price bucket:")
    print("="*78)
    buckets_yes = defaultdict(lambda: {"n":0, "wins":0, "pnl":0.0})
    for q in quotes:
        ya = q["yes_ask"]
        if ya is None or ya <= 0 or ya >= 100:
            continue
        bucket = (ya // 5) * 5  # 5-cent buckets
        b = buckets_yes[bucket]
        b["n"] += 1
        won = q["result_yes"]
        if won: b["wins"] += 1
        f = fee_cents(ya)
        b["pnl"] += (100 - ya - f) if won else (-ya)

    print(f"{'price':<10}{'n':>6}{'wins':>6}{'WR%':>8}"
          f"{'expected_WR%':>14}{'edge_pp':>10}{'avg_PnL':>10}{'EV%':>8}")
    print("-"*72)
    for k in sorted(buckets_yes):
        b = buckets_yes[k]
        if b["n"] < 10: continue
        wr = 100*b["wins"]/b["n"]
        expected = k + 2.5  # midpoint of 5c bucket
        edge = wr - expected
        avg_pnl = b["pnl"]/b["n"]
        ev_pct = (avg_pnl / (k+2.5)) * 100 if (k+2.5) > 0 else 0
        print(f"{k:>3}-{k+4:<5} {b['n']:>5}{b['wins']:>6}{wr:>7.1f}%"
              f"{expected:>13.1f}%{edge:>+9.1f}{avg_pnl:>+9.1f}c{ev_pct:>+7.1f}%")

    # ========== NO-side calibration ==========
    print("\n" + "="*78)
    print("NO SIDE CALIBRATION  (buy NO @ no_ask = 100 - yes_bid)")
    print("How often NO-buyers WIN at each price bucket:")
    print("="*78)
    buckets_no = defaultdict(lambda: {"n":0, "wins":0, "pnl":0.0})
    for q in quotes:
        no_ask = 100 - q["yes_bid"]
        if no_ask <= 0 or no_ask >= 100:
            continue
        bucket = (no_ask // 5) * 5
        b = buckets_no[bucket]
        b["n"] += 1
        won = (not q["result_yes"])
        if won: b["wins"] += 1
        f = fee_cents(no_ask)
        b["pnl"] += (100 - no_ask - f) if won else (-no_ask)

    print(f"{'price':<10}{'n':>6}{'wins':>6}{'WR%':>8}"
          f"{'expected_WR%':>14}{'edge_pp':>10}{'avg_PnL':>10}{'EV%':>8}")
    print("-"*72)
    for k in sorted(buckets_no):
        b = buckets_no[k]
        if b["n"] < 10: continue
        wr = 100*b["wins"]/b["n"]
        expected = k + 2.5
        edge = wr - expected
        avg_pnl = b["pnl"]/b["n"]
        ev_pct = (avg_pnl / (k+2.5)) * 100 if (k+2.5) > 0 else 0
        print(f"{k:>3}-{k+4:<5} {b['n']:>5}{b['wins']:>6}{wr:>7.1f}%"
              f"{expected:>13.1f}%{edge:>+9.1f}{avg_pnl:>+9.1f}c{ev_pct:>+7.1f}%")

    # ========== Spread analysis ==========
    print("\n" + "="*78)
    print("SPREAD ANALYSIS  (yes_ask - yes_bid distribution)")
    print("="*78)
    spreads = []
    for q in quotes:
        if q["yes_ask"] is not None and q["yes_bid"] is not None:
            sp = q["yes_ask"] - q["yes_bid"]
            spreads.append(sp)
    spreads.sort()
    if spreads:
        n = len(spreads)
        print(f"  n={n}  median={spreads[n//2]}c  mean={sum(spreads)/n:.1f}c"
              f"  p25={spreads[n//4]}c  p75={spreads[3*n//4]}c  p95={spreads[int(0.95*n)]}c")

    # Spread by yes_ask bucket
    print("\n  Spread (cents) by yes_ask bucket:")
    print(f"  {'yes_ask':<10}{'n':>6}{'median_sp':>11}{'p75_sp':>9}")
    sp_by_bucket = defaultdict(list)
    for q in quotes:
        if q["yes_ask"] is not None and q["yes_bid"] is not None:
            ya = q["yes_ask"]
            sp = ya - q["yes_bid"]
            bucket = (ya // 10) * 10
            sp_by_bucket[bucket].append(sp)
    for k in sorted(sp_by_bucket):
        sps = sorted(sp_by_bucket[k])
        if len(sps) < 10: continue
        print(f"  {k:>3}-{k+9:<5} {len(sps):>5}{sps[len(sps)//2]:>10}c{sps[3*len(sps)//4]:>8}c")


if __name__ == "__main__":
    main()
