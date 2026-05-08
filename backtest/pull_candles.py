"""
Pull morning candlestick prices for every settled market in our window.

For each market, we grab a single hourly candle covering 11:00-12:00 UTC
(roughly 6-7am ET). This is when the bot's same-day scanner would be
making decisions before the day's high is realized.

Output: backtest/morning_prices.csv
Columns: ticker, target_date, ts, yes_bid, yes_ask, mean_price, volume

Strategy: parallelize via threadpool; ~7000 markets at ~0.3s each = ~35 min serial,
~10 min with concurrency.
"""

import csv
import json
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
SETTLEMENTS = ROOT / "kalshi_settlements.csv"
OUT = ROOT / "morning_prices.csv"

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def fetch_candles(series, ticker, start_ts, end_ts, interval=60, retries=3):
    url = (f"{BASE}/series/{series}/markets/{ticker}/candlesticks"
           f"?start_ts={start_ts}&end_ts={end_ts}&period_interval={interval}")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wb2-backtest"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + attempt * 3)
                continue
            if e.code == 404:
                return {"candlesticks": []}
            if attempt == retries - 1:
                return {"error": str(e), "candlesticks": []}
            time.sleep(1)
        except Exception as e:
            if attempt == retries - 1:
                return {"error": str(e), "candlesticks": []}
            time.sleep(1)
    return {"candlesticks": []}


def process_market(market_row):
    """Pull morning prices for one market. Returns list of price snapshot rows."""
    series = market_row["series"]
    ticker = market_row["ticker"]
    target_date = market_row["target_date"]

    # Window: from open of trading day to close of trading day,
    # but specifically grab snapshots at 06:00, 09:00, and 12:00 ET (10/13/16 UTC).
    # In May (DST), ET is UTC-4. In Feb-Mar (EST), ET is UTC-5.
    # We use a wider 12 UTC -> 16 UTC window to catch morning trading.
    try:
        td = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return []
    # Bot operates same_day morning; the day in Kalshi terms is the day_of target_date
    # Pre-market trading on settled NYC markets opens at ~14:00 UTC on day before target.
    # The day of target_date the market is heavily traded between ~12 UTC and ~22 UTC
    # before settling that night.
    start_dt = td.replace(hour=11, minute=0, second=0)  # 7am ET on target_date
    end_dt   = td.replace(hour=18, minute=0, second=0)  # 2pm ET on target_date  (well before settle)
    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())

    res = fetch_candles(series, ticker, start_ts, end_ts, interval=60)
    candles = res.get("candlesticks", [])
    out = []
    for c in candles:
        ts_end = c.get("end_period_ts")
        price_obj = c.get("price", {}) or {}
        yb = c.get("yes_bid", {}) or {}
        ya = c.get("yes_ask", {}) or {}
        try:
            mean = price_obj.get("mean_dollars")
            close = price_obj.get("close_dollars")
            yes_bid_close = yb.get("close_dollars")
            yes_ask_close = ya.get("close_dollars")
            vol = c.get("volume_fp", "0")
            oi = c.get("open_interest_fp", "0")
        except Exception:
            continue
        out.append({
            "ticker": ticker,
            "series": series,
            "target_date": target_date,
            "ts_end": ts_end,
            "mean_price": mean if mean is not None else "",
            "close_price": close if close is not None else "",
            "yes_bid_close": yes_bid_close if yes_bid_close is not None else "",
            "yes_ask_close": yes_ask_close if yes_ask_close is not None else "",
            "volume": vol,
            "open_interest": oi,
        })
    return out


def main():
    rows = list(csv.DictReader(open(SETTLEMENTS)))
    print(f"Loaded {len(rows)} markets to query\n")

    # Open output as we go so we don't lose progress
    fields = ["ticker","series","target_date","ts_end","mean_price","close_price",
              "yes_bid_close","yes_ask_close","volume","open_interest"]
    f_out = open(OUT, "w", newline="")
    writer = csv.DictWriter(f_out, fieldnames=fields)
    writer.writeheader()

    n_snaps = 0
    errors = 0
    completed = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(process_market, r): r for r in rows}
        for fut in as_completed(futures):
            try:
                snaps = fut.result()
                for s in snaps:
                    writer.writerow(s)
                    n_snaps += 1
            except Exception as e:
                errors += 1
            completed += 1
            if completed % 200 == 0:
                elapsed = time.time() - t0
                eta = (elapsed / completed) * (len(rows) - completed)
                f_out.flush()
                print(f"  {completed}/{len(rows)}  snaps={n_snaps}  errs={errors}  elapsed={elapsed:.0f}s  eta={eta:.0f}s", flush=True)

    f_out.close()
    print(f"\nDone. Total price snaps: {n_snaps}, errors: {errors}")
    print(f"Saved -> {OUT}")


if __name__ == "__main__":
    main()
