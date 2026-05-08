"""
Exhaustively test every plausible strategy variant against the 7,242 markets
we have prices+outcomes for. The goal: find ANY price+structure combination
that is meaningfully positive-EV after fees.

Strategies tested:
  1. Pure price bands (5c slices) for YES and NO sides
  2. Strike-distance variants (close to actual vs far)
  3. Strike-type slices (greater / less / between)
  4. Bid-ask spread filters (only trade when spread is narrow/wide)
  5. Time-of-day variants (early morning vs late morning quotes)
  6. Multi-criteria intersections
"""

import csv
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
SETTLEMENTS = ROOT / "kalshi_settlements.csv"
PRICES = ROOT / "morning_prices.csv"


def fee_cents(p):
    if p is None or p <= 0 or p >= 100: return 0.0
    pd_ = p/100.0
    return math.ceil(7*pd_*(1-pd_))


def load_data():
    settle = {r["ticker"]: r for r in csv.DictReader(open(SETTLEMENTS))}
    pbt = defaultdict(list)
    for r in csv.DictReader(open(PRICES)):
        pbt[r["ticker"]].append(r)

    quotes = []
    for tk, mkt in settle.items():
        snaps = sorted(pbt.get(tk, []), key=lambda r: int(r.get("ts_end") or 0))
        # Get morning snapshot (first non-trivial spread)
        morning = None
        late = None
        for s in snaps:
            try:
                yb = s.get("yes_bid_close"); ya = s.get("yes_ask_close")
                if yb in ("",None) or ya in ("",None): continue
                yb_c = round(float(yb)*100); ya_c = round(float(ya)*100)
                if yb_c == 0 and ya_c == 100: continue
                if morning is None:
                    morning = (yb_c, ya_c, int(s["ts_end"]))
                late = (yb_c, ya_c, int(s["ts_end"]))
            except Exception:
                continue
        if morning is None: continue

        try:
            strike_low = float(mkt["floor_strike"]) if mkt["floor_strike"] else None
            strike_high = float(mkt["cap_strike"]) if mkt["cap_strike"] else None
            actual = float(mkt["expiration_value"])
        except Exception:
            continue

        if mkt["strike_type"] == "greater":
            ref = strike_low; dist = actual - strike_low
        elif mkt["strike_type"] == "less":
            ref = strike_high; dist = strike_high - actual
        elif mkt["strike_type"] == "between":
            mid = (strike_low+strike_high)/2; ref = mid; dist = actual - mid
        else:
            continue

        quotes.append({
            "ticker": tk,
            "city": mkt["city"],
            "strike_type": mkt["strike_type"],
            "strike_ref": ref,
            "strike_low": strike_low,
            "strike_high": strike_high,
            "actual": actual,
            "result_yes": (mkt["result"] == "yes"),
            "morning_yb": morning[0],
            "morning_ya": morning[1],
            "morning_ts": morning[2],
            "late_yb": late[0],
            "late_ya": late[1],
            "late_ts": late[2],
        })
    return quotes


def evaluate(name, trades):
    """Print n, WR%, total P&L, per-trade EV."""
    if not trades:
        print(f"  {name:<60s} 0 trades")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    wr = 100*wins/n
    pnl = sum(t["pnl"] for t in trades)
    ev = pnl/n
    avg_entry = sum(t["entry"] for t in trades)/n
    ev_pct = (ev/avg_entry*100) if avg_entry > 0 else 0
    flag = "" if abs(ev_pct) < 5 else ("  ★★ " if ev_pct > 5 else "")
    print(f"  {name:<60s} n={n:>5} wr={wr:>5.1f}% pnl={pnl:>+7.0f}c  ev={ev:>+5.1f}c  ev%={ev_pct:>+5.1f}%{flag}")


def trade_yes(q, side, entry):
    won = (side == "yes" and q["result_yes"]) or (side == "no" and not q["result_yes"])
    f = fee_cents(entry)
    pnl = (100 - entry - f) if won else -entry
    return {"won": won, "pnl": pnl, "entry": entry, "side": side, "ticker": q["ticker"], "city": q["city"]}


def main():
    quotes = load_data()
    print(f"Loaded {len(quotes)} markets with morning quotes\n")

    # ============ 1. Pure price-band strategies (5c slices, both sides) =============
    print("="*100)
    print("1. PURE PRICE BANDS (5c slices)  — buy YES at this price band always")
    print("="*100)
    for lo in range(0, 100, 5):
        hi = lo + 4
        trades = [trade_yes(q, "yes", q["morning_ya"]) for q in quotes
                  if lo <= q["morning_ya"] <= hi and q["morning_ya"] > 0 and q["morning_ya"] < 100]
        evaluate(f"YES @ {lo:>2}-{hi:<2}c", trades)

    print()
    print("PURE PRICE BANDS — buy NO at this price band always")
    for lo in range(0, 100, 5):
        hi = lo + 4
        trades = []
        for q in quotes:
            no_ask = 100 - q["morning_yb"]
            if lo <= no_ask <= hi and no_ask > 0 and no_ask < 100:
                trades.append(trade_yes(q, "no", no_ask))
        evaluate(f"NO  @ {lo:>2}-{hi:<2}c", trades)

    # ============ 2. Strike-distance filtered =============
    print()
    print("="*100)
    print("2. STRIKE-DISTANCE FILTERED (use ground-truth distance — UPPER BOUND only)")
    print("    'distance from realized TMAX' is post-hoc; this shows what's achievable")
    print("    if you had a perfect forecast, not what's tradeable in real time.")
    print("="*100)

    # YES side, by realized distance (positive means actual exceeded strike for `greater`,
    # actual was below cap for `less`, actual landed in band for `between`)
    print("\nYES @ 70-92c, by signed dist-from-strike (perfect-forecast simulation):")
    for d_lo, d_hi in [(-2,0),(0,2),(2,4),(4,8),(8,30),(-8,-2),(-30,-8)]:
        trades = []
        for q in quotes:
            if not (70 <= q["morning_ya"] <= 92): continue
            if q["strike_type"] in ("greater","less"):
                dist = q["actual"] - q["strike_ref"] if q["strike_type"]=="greater" else q["strike_ref"]-q["actual"]
            else:
                dist = q["actual"] - q["strike_ref"]
            if d_lo <= dist <= d_hi:
                trades.append(trade_yes(q, "yes", q["morning_ya"]))
        evaluate(f"  dist {d_lo:>+3} to {d_hi:>+3}", trades)

    # ============ 3. Strike-type slice =============
    print()
    print("="*100)
    print("3. STRIKE-TYPE SLICED")
    print("="*100)
    for st in ("greater","less","between"):
        # Cheap YES tails
        trades = [trade_yes(q,"yes",q["morning_ya"]) for q in quotes
                  if q["strike_type"]==st and 5<=q["morning_ya"]<=15]
        evaluate(f"{st:<10} YES @  5-15c (deep tail long)", trades)
        # Mid YES
        trades = [trade_yes(q,"yes",q["morning_ya"]) for q in quotes
                  if q["strike_type"]==st and 35<=q["morning_ya"]<=45]
        evaluate(f"{st:<10} YES @ 35-45c (slight underdog)", trades)
        trades = [trade_yes(q,"yes",q["morning_ya"]) for q in quotes
                  if q["strike_type"]==st and 80<=q["morning_ya"]<=92]
        evaluate(f"{st:<10} YES @ 80-92c (high-conviction lockin)", trades)
        # NO  tails
        trades = []
        for q in quotes:
            if q["strike_type"]!=st: continue
            no_ask = 100 - q["morning_yb"]
            if 5<=no_ask<=15:
                trades.append(trade_yes(q,"no",no_ask))
        evaluate(f"{st:<10} NO  @  5-15c (deep tail short)", trades)
        trades = []
        for q in quotes:
            if q["strike_type"]!=st: continue
            no_ask = 100 - q["morning_yb"]
            if 80<=no_ask<=92:
                trades.append(trade_yes(q,"no",no_ask))
        evaluate(f"{st:<10} NO  @ 80-92c (high-conviction NO lockin)", trades)
        print()

    # ============ 4. Spread filters =============
    print("="*100)
    print("4. SPREAD-FILTERED (only trade markets with TIGHT spreads <= 2c)")
    print("="*100)
    for lo in [10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90]:
        trades = []
        for q in quotes:
            if (q["morning_ya"] - q["morning_yb"]) > 2: continue
            if lo <= q["morning_ya"] <= lo+4:
                trades.append(trade_yes(q,"yes",q["morning_ya"]))
        evaluate(f"YES @ {lo:>2}-{lo+4:<2}c (tight spread <=2c)", trades)

    print()
    for lo in [10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90]:
        trades = []
        for q in quotes:
            no_ask = 100 - q["morning_yb"]
            if (q["morning_ya"] - q["morning_yb"]) > 2: continue
            if lo <= no_ask <= lo+4:
                trades.append(trade_yes(q,"no",no_ask))
        evaluate(f"NO  @ {lo:>2}-{lo+4:<2}c (tight spread <=2c)", trades)

    # ============ 5. Time-of-day: late morning vs early morning =============
    print()
    print("="*100)
    print("5. EARLY vs LATE MORNING (first vs last candle in 11-18 UTC window)")
    print("="*100)
    print("\nEARLY morning quotes:")
    for lo in [15,20,25,30,40,50,65,70,75,80,85,90]:
        trades = []
        for q in quotes:
            if lo <= q["morning_ya"] <= lo+4:
                trades.append(trade_yes(q,"yes",q["morning_ya"]))
        evaluate(f"  YES @ {lo}-{lo+4}c (early)", trades)

    print("\nLATE morning quotes (just before settle, after some weather observed):")
    for lo in [15,20,25,30,40,50,65,70,75,80,85,90]:
        trades = []
        for q in quotes:
            if lo <= q["late_ya"] <= lo+4 and q["late_ya"] > 0 and q["late_ya"] < 100:
                trades.append(trade_yes(q,"yes",q["late_ya"]))
        evaluate(f"  YES @ {lo}-{lo+4}c (late)", trades)

    # ============ 6. Sells / contrarian patterns =============
    print()
    print("="*100)
    print("6. PRICE-MOVEMENT PATTERNS")
    print("="*100)

    # Markets where price moved from morning to late: did they regress to truth?
    movers_up = []   # yes_ask rose
    movers_down = [] # yes_ask fell
    for q in quotes:
        if q["late_ya"] is None or q["morning_ya"] is None: continue
        delta = q["late_ya"] - q["morning_ya"]
        if delta >= 10:
            movers_up.append(q)
        elif delta <= -10:
            movers_down.append(q)

    # Buy YES on markets that moved UP 10+ cents — momentum continues?
    trades = [trade_yes(q,"yes",q["late_ya"]) for q in movers_up if 30<=q["late_ya"]<=70]
    evaluate("MOMENTUM YES @ 30-70c after +10c move", trades)
    trades = [trade_yes(q,"yes",q["morning_ya"]) for q in movers_down if 30<=q["morning_ya"]<=70]
    evaluate("FADE YES (buy at morning) on -10c later move", trades)


if __name__ == "__main__":
    main()
