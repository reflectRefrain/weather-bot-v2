"""
calibrate.py — Nightly feedback loop for model_decisions.

Runs after Kalshi contracts settle (typically ~10 PM local / midnight UTC).
For every model_decisions row where settled_high_f IS NULL and target_date
is in the past, this script:
  1. Fetches the actual observed high temperature from the Iowa State
     ASOS archive (free, no auth, covers all 18 Kalshi METAR stations).
  2. Writes settled_high_f back to model_decisions.
  3. Derives settled_outcome (YES/NO) from strike_type / strike_low / strike_high.

Once this runs, the daily report Sections 6 (forecast accuracy) and
7 (model calibration) will populate correctly.

Usage:
    python3 calibrate.py              # process all unsettled past rows
    python3 calibrate.py --dry-run    # show what would be written, no DB writes
    python3 calibrate.py --days 7     # only look back 7 days (default: 35)

Cron / Docker: run once nightly after midnight UTC, e.g.:
    0 1 * * * docker exec wb2-trader python3 /app/src/calibrate.py
"""
import argparse
import datetime as dt
import sys
import time
import urllib.request
import json
from db import conn

# ───────────────────────────────────────────────────────────────────────────────
#  METAR station → Iowa State ASOS network code map
#  Source: https://mesonet.agron.iastate.edu/request/download.phtml
# ───────────────────────────────────────────────────────────────────────────────
CITY_METAR = {
    "NYC":  "KNYC",
    "LAX":  "KLAX",
    "CHI":  "KMDW",
    "MIA":  "KMIA",
    "DEN":  "KDEN",
    "AUS":  "KAUS",
    "PHIL": "KPHL",
    "BOS":  "KBOS",
    "HOU":  "KHOU",
    "ATL":  "KATL",
    "PHX":  "KPHX",
    "DC":   "KDCA",
    "LAS":  "KLAS",
    "SAT":  "KSAT",
    "MIN":  "KMSP",
    "DAL":  "KDFW",
    "SF":   "KSFO",
    "OKC":  "KOKC",
}

# Iowa State ASOS API returns CSV with columns: station,valid,tmpf,...
# We request hourly obs for a single station/day and take the max tmpf.
ASOS_URL = (
    "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    "?station={station}"
    "&data=tmpf"
    "&year1={y}&month1={m}&day1={d}"
    "&year2={y}&month2={m}&day2={d}"
    "&tz=UTC&format=onlycomma&latlon=no&elev=no&missing=M&trace=T"
    "&direct=no&report_type=3"
)

FETCH_DELAY_S = 0.5   # be polite to Iowa State servers


def _fetch_observed_high(station: str, date_iso: str) -> float | None:
    """
    Fetch hourly METAR obs for `station` on `date_iso` from Iowa State ASOS.
    Returns the maximum tmpf (F) observed that calendar day, or None on error.

    The API returns rows like:
        KMDW,2026-05-07 00:53,54.0
        KMDW,2026-05-07 01:53,53.1
        ...
    We scan all rows and return the max numeric tmpf.
    """
    try:
        d = dt.date.fromisoformat(date_iso)
        url = ASOS_URL.format(
            station=station, y=d.year,
            m=str(d.month).zfill(2),
            d=str(d.day).zfill(2),
        )
        req = urllib.request.Request(url, headers={"User-Agent": "kalshi-weather-bot/2"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        max_t = None
        for line in raw.splitlines():
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                t = float(parts[2].strip())
                if max_t is None or t > max_t:
                    max_t = t
            except (ValueError, IndexError):
                continue
        return max_t
    except Exception as e:
        print(f"    [WARN] ASOS fetch failed {station} {date_iso}: {e}", file=sys.stderr)
        return None


def _derive_outcome(settled_high_f: float, strike_type: str,
                    strike_low, strike_high) -> str | None:
    """
    Derive YES/NO outcome from the settled temperature and the contract's
    strike definition. Uses Kalshi's 'strictly greater than' rule:
      - greater: YES if actual > strike_low
      - less:    YES if actual < strike_high (i.e. actual <= strike_high - 1 in integer temps)
      - between: YES if strike_low < actual < strike_high
    Returns 'YES', 'NO', or None if inputs are insufficient.
    """
    if settled_high_f is None:
        return None
    st = (strike_type or "").lower()
    try:
        if st == "greater" and strike_low is not None:
            return "YES" if settled_high_f > float(strike_low) else "NO"
        if st == "less" and strike_high is not None:
            return "YES" if settled_high_f < float(strike_high) else "NO"
        if st == "between" and strike_low is not None and strike_high is not None:
            lo, hi = float(strike_low), float(strike_high)
            return "YES" if lo < settled_high_f < hi else "NO"
    except Exception:
        pass
    return None


def run(dry_run: bool = False, lookback_days: int = 35):
    """
    Main calibration loop.

    Queries model_decisions for rows where:
      - settled_high_f IS NULL (not yet calibrated)
      - target_date < today (contract has expired)
      - ts >= today - lookback_days (don't look too far back)

    Groups by (city, target_date) to minimize ASOS API calls
    (one API call per city/date pair, not per decision row).
    """
    today = dt.date.today().isoformat()
    since = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()

    with conn() as c:
        rows = c.execute(
            """
            SELECT id, city, variable, target_date, strike_type, strike_low, strike_high
            FROM model_decisions
            WHERE settled_high_f IS NULL
              AND target_date IS NOT NULL
              AND target_date < ?
              AND date(ts) >= ?
            ORDER BY target_date, city
            """,
            (today, since),
        ).fetchall()

    if not rows:
        print("calibrate: nothing to update.")
        return

    # Group by (city, target_date) so we only call ASOS once per pair.
    from collections import defaultdict
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[(r["city"], r["target_date"])].append(dict(r))

    total_updated = 0
    total_failed = 0

    for (city, target_date), decision_rows in sorted(groups.items()):
        station = CITY_METAR.get(city)
        if not station:
            print(f"  [SKIP] {city} {target_date}: no METAR station mapped")
            total_failed += len(decision_rows)
            continue

        print(f"  Fetching {station} obs for {target_date} ({len(decision_rows)} rows) ...", end=" ")
        obs_high = _fetch_observed_high(station, target_date)
        time.sleep(FETCH_DELAY_S)

        if obs_high is None:
            print("FAILED")
            total_failed += len(decision_rows)
            continue

        print(f"high={obs_high:.1f}°F")

        for dr in decision_rows:
            outcome = _derive_outcome(
                obs_high,
                dr["strike_type"],
                dr["strike_low"],
                dr["strike_high"],
            )
            if dry_run:
                print(
                    f"    [DRY-RUN] id={dr['id']} {city} {target_date} "
                    f"st={dr['strike_type']} lo={dr['strike_low']} hi={dr['strike_high']} "
                    f"-> settled={obs_high:.1f} outcome={outcome}"
                )
            else:
                with conn() as c:
                    c.execute(
                        """
                        UPDATE model_decisions
                        SET settled_high_f = ?,
                            settled_outcome = ?
                        WHERE id = ?
                        """,
                        (obs_high, outcome, dr["id"]),
                    )
                total_updated += 1

    if not dry_run:
        print(f"calibrate: updated {total_updated} rows, {total_failed} failed/skipped.")
    else:
        print("calibrate: dry-run complete, no DB writes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill model_decisions with actual temps")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without touching the DB")
    parser.add_argument("--days", type=int, default=35,
                        help="How many days back to scan (default 35)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, lookback_days=args.days)
