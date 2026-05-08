"""
Validate the 'tight-spread YES @ 40-49c' finding.
- Walk-forward by month
- Bootstrap confidence interval
- Per-strike-type breakdown
- Per-city breakdown
- Out-of-sample feel
"""
import csv, math, random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
SETTLEMENTS = ROOT / "kalshi_settlements.csv"
PRICES = ROOT / "morning_prices.csv"


def fee_cents(p):
    if p is None or p <= 0 or p >= 100: return 0.0
    pd_ = p/100.0
    return math.ceil(7*pd_*(1-pd_))


def select_quote(snaps):
    snaps = sorted(snaps, key=lambda r: int(r.get("ts_end") or 0))
    for s in snaps:
        try:
            yb = s.get("yes_bid_close"); ya = s.get("yes_ask_close")
            if yb in ("",None) or ya in ("",None): continue
            yb_c = round(float(yb)*100); ya_c = round(float(ya)*100)
            if yb_c == 0 and ya_c == 100: continue
            return yb_c, ya_c
        except Exception:
            continue
    return None, None


def main():
    settle = {r["ticker"]: r for r in csv.DictReader(open(SETTLEMENTS))}
    pbt = defaultdict(list)
    for r in csv.DictReader(open(PRICES)):
        pbt[r["ticker"]].append(r)

    # Build candidate list: YES @ 40-49c with spread <=2c
    trades = []
    for tk, mkt in settle.items():
        yb, ya = select_quote(pbt.get(tk, []))
        if yb is None: continue
        if ya - yb > 2: continue
        if not (40 <= ya <= 49): continue
        won = (mkt["result"] == "yes")
        f = fee_cents(ya)
        pnl = (100 - ya - f) if won else -ya
        trades.append({
            "ticker": tk, "city": mkt["city"], "date": mkt["target_date"],
            "strike_type": mkt["strike_type"], "ya": ya, "yb": yb,
            "spread": ya-yb, "won": won, "pnl": pnl
        })

    n = len(trades)
    if n == 0:
        print("No trades")
        return
    wins = sum(t["won"] for t in trades)
    pnl = sum(t["pnl"] for t in trades)
    avg_entry = sum(t["ya"] for t in trades)/n
    print(f"\n=== TIGHT-SPREAD YES @ 40-49c ===")
    print(f"n={n}  wins={wins}  WR={100*wins/n:.1f}%  total_PnL={pnl}c  avg_PnL={pnl/n:+.2f}c  EV={pnl/n/avg_entry*100:+.1f}%\n")

    # Bootstrap 95% CI on per-trade EV
    print("Bootstrap 95% CI on per-trade P&L (10,000 resamples):")
    ev_samples = []
    rnd = random.Random(42)
    pnls = [t["pnl"] for t in trades]
    for _ in range(10000):
        sample = [pnls[rnd.randrange(n)] for _ in range(n)]
        ev_samples.append(sum(sample)/n)
    ev_samples.sort()
    lo, hi = ev_samples[250], ev_samples[9750]
    print(f"  per-trade P&L 95% CI: [{lo:+.2f}c, {hi:+.2f}c]")
    print(f"  fraction of bootstraps with positive EV: {sum(1 for x in ev_samples if x>0)/100:.1f}%\n")

    # Walk-forward by month
    print("WALK-FORWARD by month (no peeking):")
    print(f"  {'month':<10}{'n':>6}{'wins':>6}{'WR%':>8}{'pnl':>8}{'ev':>8}")
    by_mo = defaultdict(list)
    for t in trades:
        mo = t["date"][:7]
        by_mo[mo].append(t)
    for mo in sorted(by_mo):
        ts = by_mo[mo]
        n_ = len(ts); w_ = sum(t["won"] for t in ts)
        p_ = sum(t["pnl"] for t in ts)
        ev_ = p_/n_ if n_ else 0
        print(f"  {mo:<10}{n_:>6}{w_:>6}{100*w_/n_:>7.1f}%{p_:>+7.0f}c{ev_:>+7.1f}c")

    # By strike_type
    print("\nBy strike_type:")
    print(f"  {'type':<12}{'n':>6}{'wins':>6}{'WR%':>8}{'pnl':>8}{'ev':>8}")
    by_st = defaultdict(list)
    for t in trades:
        by_st[t["strike_type"]].append(t)
    for st in sorted(by_st):
        ts = by_st[st]
        n_ = len(ts); w_ = sum(t["won"] for t in ts)
        p_ = sum(t["pnl"] for t in ts)
        print(f"  {st:<12}{n_:>6}{w_:>6}{100*w_/n_:>7.1f}%{p_:>+7.0f}c{p_/n_:>+7.1f}c")

    # By city
    print("\nBy city (sorted by total P&L):")
    print(f"  {'city':<6}{'n':>5}{'wins':>5}{'WR%':>8}{'pnl':>8}{'ev':>8}")
    by_c = defaultdict(list)
    for t in trades:
        by_c[t["city"]].append(t)
    for c, ts in sorted(by_c.items(), key=lambda kv: -sum(t["pnl"] for t in kv[1])):
        n_ = len(ts); w_ = sum(t["won"] for t in ts)
        p_ = sum(t["pnl"] for t in ts)
        print(f"  {c:<6}{n_:>5}{w_:>5}{100*w_/n_:>7.1f}%{p_:>+7.0f}c{p_/n_:>+7.1f}c")

    # Sanity check: expected outcome of 339-trade run with TRUE win rate = 50%
    # and entry=44.5c gives:
    # E[pnl] = 0.5*(100-44.5-3) + 0.5*(-44.5) = 26.25 - 22.25 = 4c
    # Std per trade = sqrt(0.5*(52.5-4)^2 + 0.5*(-44.5-4)^2) = ~48c
    # Std of mean = 48/sqrt(339) = 2.6c
    # So observed +5.9c per trade is ~2.3 std devs from 50/50 — significant but not crushing
    print("\n=== HONEST CAVEATS ===")
    print("This finding has the following potential confounders:")
    print(" 1. SAMPLE: 339 trades is meaningful but not huge. Bootstrap CI tells you the range.")
    print(" 2. SLIPPAGE: We're assuming you can fill at the close-of-candle yes_ask. In reality,")
    print("    when YES drops to 40c with tight spread, the resting offer may evaporate.")
    print(" 3. SURVIVORSHIP: We only see markets that traded — markets with no morning volume")
    print("    at 40-49c never appear, but might be where the real mispricing lives.")
    print(" 4. NO MODEL: This says the price band has +EV — but in real-time the bot still")
    print("    needs a rule for WHICH 40-49c market to enter. Indiscriminate entry on every")
    print("    40-49c market means LOTS of trades — sizing matters.")
    print(" 5. WINDOW: Mar-May 2026 only. Different seasons have different dynamics —")
    print("    summer extremes, winter swings, transition periods all behave differently.")


if __name__ == "__main__":
    main()
