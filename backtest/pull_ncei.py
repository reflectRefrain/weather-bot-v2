"""
Pull 90 days of GHCN-Daily TMAX for all 18 cities.
Free NCEI API, no auth required.

Output: backtest/cli_actuals.csv
Columns: date, city, station, tmax_f, tmin_f
"""

import csv
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stations import CITIES

OUT = Path(__file__).parent / "cli_actuals.csv"

# 90-day window ending yesterday (today's TMAX may not be settled yet)
END = date.today() - timedelta(days=1)
START = END - timedelta(days=90)

URL = (
    "https://www.ncei.noaa.gov/access/services/data/v1"
    "?dataset=daily-summaries"
    "&dataTypes=TMAX,TMIN"
    "&stations={station}"
    "&startDate={start}"
    "&endDate={end}"
    "&format=json"
    "&units=standard"
)


def fetch(station: str, start: date, end: date, retries: int = 3):
    url = URL.format(station=station, start=start.isoformat(), end=end.isoformat())
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weather-bot-v2-backtest"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8")
                if not body.strip():
                    return []
                return json.loads(body)
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} on {station} attempt {attempt+1}: {e.reason}")
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if attempt == retries - 1:
                raise
            time.sleep(2)
        except Exception as e:
            print(f"  Error on {station} attempt {attempt+1}: {e}")
            if attempt == retries - 1:
                raise
            time.sleep(2)
    return []


def main():
    print(f"Pulling NCEI TMAX/TMIN: {START} -> {END}  ({(END-START).days+1} days)")
    print(f"Output: {OUT}\n")

    rows = []
    for city, metar, station, name in CITIES:
        print(f"[{city:5s}] {name} ({station})...", end=" ", flush=True)
        try:
            data = fetch(station, START, END)
        except Exception as e:
            print(f"FAILED: {e}")
            continue

        n_tmax = 0
        for rec in data:
            d = rec.get("DATE")
            tmax = rec.get("TMAX")
            tmin = rec.get("TMIN")
            if d and tmax not in (None, ""):
                try:
                    tmax_f = float(tmax)
                    tmin_f = float(tmin) if tmin not in (None, "") else None
                    rows.append({
                        "date": d,
                        "city": city,
                        "station": station,
                        "tmax_f": tmax_f,
                        "tmin_f": tmin_f if tmin_f is not None else "",
                    })
                    n_tmax += 1
                except (ValueError, TypeError):
                    pass

        print(f"{n_tmax} days")
        time.sleep(0.5)  # polite

    print(f"\nTotal rows: {len(rows)}")
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "city", "station", "tmax_f", "tmin_f"])
        w.writeheader()
        w.writerows(rows)
    print(f"Saved -> {OUT}")


if __name__ == "__main__":
    main()
