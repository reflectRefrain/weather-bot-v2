"""
Pull settled Kalshi KXHIGH markets for all 18 cities, last ~120 days.
We pull a generous window to ensure we have settlements for every actuals date.

For each settled market, we record:
  - ticker, series, event_ticker
  - target_date (parsed from ticker)
  - strike (floor or cap)
  - strike_type (greater | less | between | structured | bracket)
  - expiration_value (the official TMAX in F)
  - result (yes | no | void)
  - last_price_dollars (final mid)
  - previous_yes_bid_dollars, previous_yes_ask_dollars (last quote pre-settle)
  - close_time, open_time, settlement_ts

Output: backtest/kalshi_settlements.csv
"""

import csv
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from series_map import CITY_SERIES

OUT = Path(__file__).parent / "kalshi_settlements.csv"
BASE = "https://api.elections.kalshi.com/trade-api/v2/markets"

MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
RE_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")


def target_date_from_ticker(tk: str):
    m = RE_DATE.search(tk or "")
    if not m:
        return None
    yy = 2000 + int(m.group(1))
    mo = MONTHS.get(m.group(2))
    dd = int(m.group(3))
    if not mo:
        return None
    try:
        return f"{yy:04d}-{mo:02d}-{dd:02d}"
    except Exception:
        return None


def fetch_all_settled(series_ticker: str, max_pages: int = 50):
    """Page through all settled markets for a given series."""
    out = []
    cursor = None
    pages = 0
    while pages < max_pages:
        url = f"{BASE}?series_ticker={series_ticker}&status=settled&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "wb2-backtest"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except Exception:
                time.sleep(2)
        markets = data.get("markets", [])
        out.extend(markets)
        cursor = data.get("cursor")
        pages += 1
        if not cursor or not markets:
            break
        time.sleep(0.4)
    return out


def main():
    print(f"Pulling settled KXHIGH markets for {len(CITY_SERIES)} cities")
    print(f"Output: {OUT}\n")

    rows = []
    for city, series in CITY_SERIES.items():
        print(f"[{city:5s}] {series}...", end=" ", flush=True)
        try:
            markets = fetch_all_settled(series)
        except Exception as e:
            print(f"FAILED: {e}")
            continue
        n_kept = 0
        for m in markets:
            tk = m.get("ticker", "")
            td = target_date_from_ticker(tk)
            if not td:
                continue
            row = {
                "city": city,
                "series": series,
                "event_ticker": m.get("event_ticker", ""),
                "ticker": tk,
                "target_date": td,
                "strike_type": m.get("strike_type", ""),
                "floor_strike": m.get("floor_strike", ""),
                "cap_strike": m.get("cap_strike", ""),
                "expiration_value": m.get("expiration_value", ""),
                "result": m.get("result", ""),
                "last_price_dollars": m.get("last_price_dollars", ""),
                "previous_yes_bid_dollars": m.get("previous_yes_bid_dollars", ""),
                "previous_yes_ask_dollars": m.get("previous_yes_ask_dollars", ""),
                "open_time": m.get("open_time", ""),
                "close_time": m.get("close_time", ""),
                "settlement_ts": m.get("settlement_ts", ""),
                "subtitle": m.get("subtitle", ""),
                "volume_fp": m.get("volume_fp", ""),
            }
            rows.append(row)
            n_kept += 1
        print(f"{n_kept} markets")
        time.sleep(0.5)

    # Sort for readability
    rows.sort(key=lambda r: (r["city"], r["target_date"], r["ticker"]))

    print(f"\nTotal markets: {len(rows)}")
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"Saved -> {OUT}")


if __name__ == "__main__":
    main()
