"""
Analyze strike-vs-actual distribution from settlements data.
This tells us: for each strike type, at what 'distance from actual' did markets resolve?

KEY QUESTION: when a market's strike is N degrees from the realized TMAX,
how often does it resolve YES vs NO?

This bounds what's possible. If 'greater' markets at strike = actual + 5
resolve YES <5% of the time, then tail_long (YES @ ≤30¢) is buying a
fundamentally low-probability outcome — fees + adverse-selection guaranteed losses.
"""

import csv
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
SETTLEMENTS = ROOT / "kalshi_settlements.csv"

def main():
    rows = list(csv.DictReader(open(SETTLEMENTS)))
    print(f"Loaded {len(rows)} settled markets\n")

    # Bucket by strike_type and signed distance (strike - actual)
    by_type_dist = defaultdict(lambda: {"yes": 0, "no": 0, "void": 0})

    skipped = 0
    for r in rows:
        try:
            actual = float(r["expiration_value"])
        except Exception:
            skipped += 1
            continue
        result = r["result"]
        st = r["strike_type"]
        if st == "greater":
            try:
                strike = float(r["floor_strike"])
            except Exception: continue
            # YES wins if actual > strike  (i.e. dist = actual - strike > 0)
            dist = actual - strike
            bucket = round(dist)
            by_type_dist[("greater", bucket)][result] = by_type_dist[("greater", bucket)].get(result, 0) + 1
        elif st == "less":
            try:
                strike = float(r["cap_strike"])
            except Exception: continue
            # YES wins if actual <= strike (dist = strike - actual >= 0)
            dist = strike - actual
            bucket = round(dist)
            by_type_dist[("less", bucket)][result] = by_type_dist[("less", bucket)].get(result, 0) + 1
        elif st == "between":
            try:
                lo = float(r["floor_strike"])
                hi = float(r["cap_strike"])
            except Exception: continue
            # YES wins if lo <= actual <= hi
            mid = (lo + hi) / 2
            dist = round(actual - mid)
            by_type_dist[("between", dist)][result] = by_type_dist[("between", dist)].get(result, 0) + 1

    # Greater: print resolution rate by distance
    print("=" * 70)
    print("STRIKE_TYPE = greater   (YES wins if actual > floor_strike)")
    print(f"  dist = actual - floor_strike    (>0 means YES wins)")
    print("=" * 70)
    print(f"{'dist':>5}  {'n':>5}  {'yes':>5}  {'no':>5}  {'yes_rate':>8}")
    keys = sorted([k for k in by_type_dist if k[0] == "greater"], key=lambda x: x[1])
    for k in keys:
        d = by_type_dist[k]
        n = d.get("yes", 0) + d.get("no", 0)
        if n < 5: continue
        yr = 100 * d.get("yes", 0) / n if n else 0
        print(f"{k[1]:>5}  {n:>5}  {d.get('yes',0):>5}  {d.get('no',0):>5}  {yr:>7.1f}%")

    print("\n" + "=" * 70)
    print("STRIKE_TYPE = less   (YES wins if actual <= cap_strike)")
    print(f"  dist = cap_strike - actual    (>=0 means YES wins)")
    print("=" * 70)
    print(f"{'dist':>5}  {'n':>5}  {'yes':>5}  {'no':>5}  {'yes_rate':>8}")
    keys = sorted([k for k in by_type_dist if k[0] == "less"], key=lambda x: x[1])
    for k in keys:
        d = by_type_dist[k]
        n = d.get("yes", 0) + d.get("no", 0)
        if n < 5: continue
        yr = 100 * d.get("yes", 0) / n if n else 0
        print(f"{k[1]:>5}  {n:>5}  {d.get('yes',0):>5}  {d.get('no',0):>5}  {yr:>7.1f}%")

    print("\n" + "=" * 70)
    print("STRIKE_TYPE = between   (YES wins if floor <= actual <= cap)")
    print(f"  dist = round(actual - midpoint)")
    print("=" * 70)
    print(f"{'dist':>5}  {'n':>5}  {'yes':>5}  {'no':>5}  {'yes_rate':>8}")
    keys = sorted([k for k in by_type_dist if k[0] == "between"], key=lambda x: x[1])
    for k in keys:
        d = by_type_dist[k]
        n = d.get("yes", 0) + d.get("no", 0)
        if n < 5: continue
        yr = 100 * d.get("yes", 0) / n if n else 0
        print(f"{k[1]:>5}  {n:>5}  {d.get('yes',0):>5}  {d.get('no',0):>5}  {yr:>7.1f}%")

if __name__ == "__main__":
    main()
