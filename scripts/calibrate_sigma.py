#!/usr/bin/env python3
"""
Empirical sigma calibration (Tier 1.2 from PROFITABLE_TRADER_REVIEW.md).

READ-ONLY analysis. Reads model_decisions joined with the resolved outcome
(settled_high_f), computes the actual forecast error per (city, horizon),
and writes per-bucket RMSE to data/sigma_empirical.json.

This is the empirical replacement for the hard-coded SIGMA dict in model.py.
We don't wire it in yet — model.py keeps its conservative defaults until we
have at least N_MIN_PER_BUCKET samples per bucket, otherwise the model
becomes self-referentially miscalibrated.

Usage:
    python scripts/calibrate_sigma.py
    python scripts/calibrate_sigma.py --db /app/data/bot.db
    python scripts/calibrate_sigma.py --dry-run     # print only, don't write

Inputs:
    model_decisions table, rows where settled_high_f IS NOT NULL.
    Schema fields used: city, horizon, variable, effective_forecast,
                        settled_high_f, ts.

Outputs (JSON):
    {
      "generated_at": "ISO timestamp UTC",
      "sample_count_total": N,
      "buckets": {
        "CHI:HIGHTEMP:same_day": {
          "n": 47,
          "rmse_f": 2.84,
          "mean_bias_f": -0.31,
          "p50_abs_err_f": 2.10,
          "p90_abs_err_f": 4.55,
          "first_ts": "2026-05-01T...",
          "last_ts":  "2026-05-15T..."
        },
        ...
      }
    }

Bucket key format: "{city}:{variable}:{horizon}".

Sample-size thresholds:
    n < 10  -> reported but flagged "insufficient"
    n >= 30 -> safe to use empirically
    n >= 100 -> high confidence

Future: model.py reads sigma_empirical.json on import (with fallback to
hard-coded SIGMA). Wiring deferred until we have data for ≥1 bucket at
n>=30.
"""
import argparse
import datetime as dt
import json
import math
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

DEFAULT_DB = "/app/data/bot.db"
DEFAULT_OUT = "data/sigma_empirical.json"
N_MIN_PER_BUCKET = 10  # below this we don't even report (noise floor)


def fetch_resolved_decisions(db_path):
    """Yield rows: (city, variable, horizon, effective_forecast, settled_high_f, ts).
    Filters: settled_high_f IS NOT NULL, effective_forecast IS NOT NULL,
             variable IN ('HIGHTEMP','LOWTEMP') (sigma is most actionable here),
             horizon != 'expired'.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        # Schema check — older DBs may not have settled_high_f.
        cols = [r[1] for r in c.execute("PRAGMA table_info(model_decisions)").fetchall()]
        if "settled_high_f" not in cols:
            return []
        rows = c.execute(
            """
            SELECT city, variable, horizon, effective_forecast,
                   settled_high_f, ts
              FROM model_decisions
             WHERE settled_high_f IS NOT NULL
               AND effective_forecast IS NOT NULL
               AND variable IN ('HIGHTEMP','LOWTEMP')
               AND horizon != 'expired'
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def percentile(values, p):
    """Simple linear-interpolation percentile (no numpy dep)."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c_ = math.ceil(k)
    if f == c_:
        return s[int(k)]
    return s[f] + (s[c_] - s[f]) * (k - f)


def calibrate(rows):
    """Group by (city, variable, horizon) and compute error stats."""
    buckets = defaultdict(list)  # key -> list of (err, ts)
    for r in rows:
        forecast = r["effective_forecast"]
        actual = r["settled_high_f"]
        if forecast is None or actual is None:
            continue
        err = float(actual) - float(forecast)  # signed: + means forecast was too cool
        key = f"{r['city']}:{r['variable']}:{r['horizon']}"
        buckets[key].append((err, r["ts"]))

    out = {}
    for key, samples in buckets.items():
        if len(samples) < N_MIN_PER_BUCKET:
            continue
        errs = [s[0] for s in samples]
        abs_errs = [abs(e) for e in errs]
        n = len(errs)
        mean_err = sum(errs) / n
        # RMSE (root mean square of errors — what sigma is supposed to model)
        rmse = math.sqrt(sum(e * e for e in errs) / n)
        out[key] = {
            "n": n,
            "rmse_f": round(rmse, 3),
            "mean_bias_f": round(mean_err, 3),
            "p50_abs_err_f": round(percentile(abs_errs, 50) or 0, 3),
            "p90_abs_err_f": round(percentile(abs_errs, 90) or 0, 3),
            "first_ts": min(s[1] for s in samples),
            "last_ts": max(s[1] for s in samples),
            "confidence": (
                "high" if n >= 100 else
                "medium" if n >= 30 else
                "low"
            ),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DB_PATH", DEFAULT_DB),
                    help=f"SQLite DB path (default: {DEFAULT_DB} or $DB_PATH)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"Output JSON path (default: {DEFAULT_OUT})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print results, don't write file")
    args = ap.parse_args()

    print(f"[calibrate_sigma] reading from {args.db}")
    try:
        rows = fetch_resolved_decisions(args.db)
    except FileNotFoundError as e:
        print(f"[calibrate_sigma] {e}")
        return 1

    print(f"[calibrate_sigma] found {len(rows)} resolved decisions")
    buckets = calibrate(rows)

    if not buckets:
        print(f"[calibrate_sigma] no buckets meet n>={N_MIN_PER_BUCKET} threshold yet.")
        print(f"[calibrate_sigma] keep the bot running — we need ~2 weeks of data.")
        if not args.dry_run:
            # Still write an empty payload so downstream tooling can detect
            # the file's presence.
            payload = {
                "generated_at": dt.datetime.utcnow().isoformat() + "Z",
                "sample_count_total": len(rows),
                "buckets": {},
                "note": f"insufficient data (need {N_MIN_PER_BUCKET}+ per bucket)",
            }
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2))
            print(f"[calibrate_sigma] wrote empty stub to {out_path}")
        return 0

    payload = {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "sample_count_total": len(rows),
        "buckets": buckets,
    }
    print()
    print(f"{'BUCKET':40} {'N':>5} {'RMSE':>7} {'BIAS':>7} {'P50':>7} {'P90':>7} CONF")
    print("-" * 88)
    for key in sorted(buckets):
        b = buckets[key]
        print(f"{key:40} {b['n']:>5} {b['rmse_f']:>7.2f} "
              f"{b['mean_bias_f']:>+7.2f} {b['p50_abs_err_f']:>7.2f} "
              f"{b['p90_abs_err_f']:>7.2f} {b['confidence']}")
    print()

    if args.dry_run:
        print("[calibrate_sigma] --dry-run, not writing")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"[calibrate_sigma] wrote {out_path}")
    print(f"[calibrate_sigma] NOTE: model.py does NOT yet read this file.")
    print(f"[calibrate_sigma] Wiring deferred until any bucket reaches n>=30.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
