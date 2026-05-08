"""
Backtest replay engine — price-band-only strategy simulation.

KEY METHODOLOGY (read this before judging results):
We do NOT have the NWS+NBM forecasts that the bot used at decision time.
Without them, we cannot replicate the bot's `mp_yes` calculation, which
gates whether a candidate enters.

What we CAN test rigorously: the *price band gates* of each strategy.
That answers a structural question: "Of all the markets that fell into the
strategy's price/structure window, what % win rate did they deliver?"

This is a STRICT UPPER BOUND on strategy performance. If even taking every
price-band qualifier is unprofitable after fees, no model can save the
strategy. Conversely, if the price band is profitable, then the bot's
losses are a model-quality problem, not a strategy-design problem.

STRATEGIES TESTED (price gates only):
  tail_short:  buy NO @ 15-30c, where strike >= forecast+4F
               (no-forecast version: just NO @ 15-30c, any strike type)
  tail_long:   buy YES @ <=30c (mirror of tail_short)
  lockin_NO:   buy NO when YES price >= 70c (i.e. NO @ <=30c, but high-conviction direction)
               -- NOTE: this is functionally similar to tail_long; the live bot's
                  lockin path requires forecast match. We test the price shape only.
  lockin_YES:  buy YES @ 70-92c (high-conviction long)
  legacy:      buy either side @ 30-70c (the "edge" middle band)

For each strategy we evaluate on every (ticker, target_date) we have prices for.
We use the 11:00-12:00 UTC candle (~7am ET on target day) as the entry quote.
We assume a market order at the yes_ask (or no_ask = 100 - yes_bid) at that time.

Fee model (Kalshi): fee_cents = ceil(7 * P * (1-P))   where P is entry price in dollars.
P&L per contract:
  WIN:   100 - entry_cents - fee_cents
  LOSS:  -entry_cents       (fee on losing trade is 0; only winning side pays the trade fee
                              -- actually Kalshi charges fees on entry only, not exit;
                              we model fee as paid on entry regardless of outcome.)

Per Kalshi docs (verified earlier this session): fees are charged ONCE per trade,
on entry, equal to ceil(0.07 * contracts * P * (1-P)) rounded up to next cent.
"""

import csv
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
SETTLEMENTS = ROOT / "kalshi_settlements.csv"
PRICES = ROOT / "morning_prices.csv"
OUT = ROOT / "replay_results.csv"
OUT_SUMMARY = ROOT / "replay_summary.txt"


def fee_cents(price_cents: float) -> float:
    """Kalshi fee: ceil(7 * P * (1-P)) where P is in dollars."""
    if price_cents is None or price_cents <= 0 or price_cents >= 100:
        return 0.0
    p = price_cents / 100.0
    return math.ceil(7 * p * (1 - p))


def market_resolves_yes(strike_type, floor_strike, cap_strike, expiration_value, result):
    """Use the recorded `result` field — already authoritative."""
    return result == "yes"


def select_morning_quote(prices_for_ticker):
    """From the per-hour candles for a ticker on its target date, choose the
    earliest one that has both yes_bid and yes_ask close prices.
    Returns (yes_bid, yes_ask, mean_price) in cents, or (None,None,None)."""
    # Sort by ts_end ascending
    snaps = sorted(prices_for_ticker, key=lambda r: int(r.get("ts_end") or 0))
    for s in snaps:
        try:
            yb = s.get("yes_bid_close")
            ya = s.get("yes_ask_close")
            mp = s.get("mean_price") or s.get("close_price")
            if yb in ("", None) or ya in ("", None):
                continue
            yb_c = round(float(yb) * 100)
            ya_c = round(float(ya) * 100)
            mp_c = round(float(mp) * 100) if mp not in ("", None) else None
            if yb_c == 0 and ya_c == 100:
                # Pure no-trade spread (post-settle marks); skip
                continue
            return yb_c, ya_c, mp_c
        except Exception:
            continue
    return None, None, None


# Strategy gate functions: take (yes_bid, yes_ask, strike_type) -> (side, entry_price_cents) or None
def gate_tail_short(yb, ya, st):
    """tail_short: buy NO @ 15-30c. NO ask = 100 - yes_bid."""
    if yb is None: return None
    no_ask = 100 - yb
    if 15 <= no_ask <= 30:
        return ("no", no_ask)
    return None


def gate_tail_long(yb, ya, st):
    """tail_long: buy YES @ <=30c (and >=15 to avoid 1c noise)."""
    if ya is None: return None
    if 15 <= ya <= 30:
        return ("yes", ya)
    return None


def gate_lockin_yes(yb, ya, st):
    """lockin_yes: buy YES @ 70-92c."""
    if ya is None: return None
    if 70 <= ya <= 92:
        return ("yes", ya)
    return None


def gate_lockin_no(yb, ya, st):
    """lockin_no: NO @ 70-92c (i.e. yes_bid <= 30)... wait, NO ask = 100-yes_bid.
    NO ask <= 30 means yes_bid >= 70 — but that's a contradiction in spreads.
    The live bot's lockin_NO entries that we saw came from buying NO at 70-92c
    (no_ask 70-92 means yes_bid <= 30). That makes the conviction directional
    but on the NO side. Let's gate on no_ask in [70,92]."""
    if yb is None: return None
    no_ask = 100 - yb
    if 70 <= no_ask <= 92:
        return ("no", no_ask)
    return None


def gate_legacy(yb, ya, st):
    """legacy: buy YES @ 30-70 OR NO @ 30-70.  We pick whichever side has the better
    (cheaper) entry. If both are 30-70 (spread-symmetric), prefer YES."""
    if ya is None or yb is None: return None
    if 30 <= ya <= 70:
        return ("yes", ya)
    no_ask = 100 - yb
    if 30 <= no_ask <= 70:
        return ("no", no_ask)
    return None


STRATEGIES = {
    "tail_short": gate_tail_short,
    "tail_long":  gate_tail_long,
    "lockin_yes": gate_lockin_yes,
    "lockin_no":  gate_lockin_no,
    "legacy":     gate_legacy,
}


def main():
    # Load settlements
    settlements = list(csv.DictReader(open(SETTLEMENTS)))
    settle_by_ticker = {r["ticker"]: r for r in settlements}
    print(f"Loaded {len(settlements)} settlements")

    # Load prices and group by ticker
    prices_by_ticker = defaultdict(list)
    n_price_rows = 0
    for r in csv.DictReader(open(PRICES)):
        prices_by_ticker[r["ticker"]].append(r)
        n_price_rows += 1
    print(f"Loaded {n_price_rows} price snapshots covering {len(prices_by_ticker)} tickers")

    # Replay
    out_rows = []
    n_no_quote = 0
    for tk, mkt in settle_by_ticker.items():
        snaps = prices_by_ticker.get(tk, [])
        yb, ya, mp_c = select_morning_quote(snaps)
        if yb is None:
            n_no_quote += 1
            continue
        st = mkt["strike_type"]
        result_yes = (mkt["result"] == "yes")

        for sname, gate in STRATEGIES.items():
            sig = gate(yb, ya, st)
            if sig is None:
                continue
            side, entry = sig
            won = (side == "yes" and result_yes) or (side == "no" and not result_yes)
            f = fee_cents(entry)
            pnl_per = (100 - entry - f) if won else (-entry)
            out_rows.append({
                "ticker": tk,
                "city": mkt["city"],
                "target_date": mkt["target_date"],
                "strike_type": st,
                "strategy": sname,
                "side": side,
                "entry_cents": entry,
                "fee_cents": f,
                "result": mkt["result"],
                "won": int(won),
                "pnl_per_contract": pnl_per,
                "yes_bid": yb,
                "yes_ask": ya,
                "expiration_value": mkt["expiration_value"],
            })

    print(f"\nMarkets without usable morning quote: {n_no_quote}")
    print(f"Total simulated entries: {len(out_rows)}")

    # Save detail
    if out_rows:
        with open(OUT, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(out_rows)
        print(f"Saved detail -> {OUT}")

    # Summarize
    summary_lines = []

    def write(s=""):
        print(s)
        summary_lines.append(s)

    write(f"\n{'='*78}")
    write("PRICE-BAND BACKTEST RESULTS  (no model — strict upper bound)")
    write(f"{'='*78}")
    write(f"Markets tested: {len(settle_by_ticker)}  |  with quotes: {len(set(r['ticker'] for r in out_rows))}")
    write(f"Total simulated entries (strategy x ticker, all strategies): {len(out_rows)}\n")

    by_strat = defaultdict(list)
    for r in out_rows:
        by_strat[r["strategy"]].append(r)

    write(f"{'Strategy':<14}{'n':>6}{'wins':>6}{'losses':>8}{'WR%':>8}"
          f"{'avg_entry':>11}{'avg_fee':>10}{'pnl_total':>12}{'pnl_per':>10}{'EV%':>8}")
    write("-"*100)
    for sname in ["tail_short", "tail_long", "lockin_no", "lockin_yes", "legacy"]:
        rows = by_strat.get(sname, [])
        if not rows:
            write(f"{sname:<14}{0:>6}")
            continue
        n = len(rows)
        wins = sum(r["won"] for r in rows)
        losses = n - wins
        wr = 100 * wins / n
        avg_entry = sum(r["entry_cents"] for r in rows) / n
        avg_fee = sum(r["fee_cents"] for r in rows) / n
        pnl_total = sum(r["pnl_per_contract"] for r in rows)
        pnl_per = pnl_total / n
        # EV% = pnl_per_contract / avg_entry  (return on risk per contract)
        ev_pct = (pnl_per / avg_entry * 100) if avg_entry > 0 else 0
        write(f"{sname:<14}{n:>6}{wins:>6}{losses:>8}{wr:>7.1f}%"
              f"{avg_entry:>10.1f}c{avg_fee:>9.1f}c{pnl_total:>10.0f}c{pnl_per:>+8.1f}c{ev_pct:>+7.1f}%")
    write("")

    # Per-city breakdown for tail_short and lockin_no (the "real signal" candidates)
    for sname in ["tail_short", "lockin_no", "tail_long"]:
        rows = by_strat.get(sname, [])
        if not rows: continue
        write(f"\n{sname} per-city  (sorted by total P&L):")
        write(f"  {'city':<6}{'n':>5}{'wins':>6}{'WR%':>7}{'pnl_total':>12}{'pnl_per':>10}")
        per_c = defaultdict(list)
        for r in rows:
            per_c[r["city"]].append(r)
        ranked = sorted(per_c.items(), key=lambda kv: -sum(r["pnl_per_contract"] for r in kv[1]))
        for city, rs in ranked:
            n = len(rs); wins = sum(r["won"] for r in rs)
            wr = 100*wins/n
            pnl = sum(r["pnl_per_contract"] for r in rs)
            write(f"  {city:<6}{n:>5}{wins:>6}{wr:>6.1f}%{pnl:>10.0f}c{pnl/n:>+8.1f}c")

    # Strike-type breakdown for tail_short and tail_long
    for sname in ["tail_short", "tail_long"]:
        rows = by_strat.get(sname, [])
        if not rows: continue
        write(f"\n{sname} by strike_type:")
        write(f"  {'strike_type':<14}{'n':>6}{'wins':>6}{'WR%':>8}{'pnl_per':>10}")
        per_st = defaultdict(list)
        for r in rows:
            per_st[r["strike_type"]].append(r)
        for st_, rs in sorted(per_st.items()):
            n = len(rs); wins = sum(r["won"] for r in rs)
            wr = 100*wins/n
            pnl = sum(r["pnl_per_contract"] for r in rs)
            write(f"  {st_:<14}{n:>6}{wins:>6}{wr:>7.1f}%{pnl/n:>+8.1f}c")

    # Save summary
    with open(OUT_SUMMARY, "w") as f:
        f.write("\n".join(summary_lines))
    write(f"\nSaved summary -> {OUT_SUMMARY}")


if __name__ == "__main__":
    main()
