"""Strategy + contract performance review. Run inside the wb2-trader container.

Usage:
    docker exec wb2-trader python3 /app/scripts/strategy_review.py
"""
import sqlite3
import os

DB = os.getenv("DB_PATH", "/app/data/bot.db")
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row


def q(s, *a):
    return c.execute(s, a).fetchall()


def classify(side, entry):
    if side == "no" and entry <= 30:
        return "tail_short"
    if side == "no" and entry >= 70:
        return "lockin_NO"
    if side == "yes" and entry >= 70:
        return "lockin_YES"
    if side == "yes" and entry <= 30:
        return "tail_long"
    return "legacy"


# ---- 1. Strategy performance ----
print("\n===== STRATEGY PERFORMANCE =====\n")
print(f"{'strategy':<22} {'n':>4} {'W':>4} {'L':>4} {'WR%':>6} {'P&L':>10} {'avg':>8}")
print("-" * 68)
buckets = q("""
    SELECT
        CASE WHEN side='no'  AND entry_cents<=30 THEN 'tail_short (NO 15-30)'
             WHEN side='no'  AND entry_cents>=70 THEN 'lockin_NO (NO >=70)'
             WHEN side='yes' AND entry_cents>=70 THEN 'lockin_YES (YES >=70)'
             WHEN side='yes' AND entry_cents<=30 THEN 'tail_long (YES <=30)'
             ELSE 'legacy_middle (30-70)' END AS b,
        COUNT(*) AS n,
        SUM(CASE WHEN realized_usd>0 THEN 1 ELSE 0 END) AS w,
        SUM(CASE WHEN realized_usd<0 THEN 1 ELSE 0 END) AS l,
        ROUND(SUM(realized_usd), 2) AS p,
        ROUND(AVG(realized_usd), 3) AS a
    FROM pnl GROUP BY b ORDER BY p DESC
""")
total_p, total_n, total_w = 0.0, 0, 0
for r in buckets:
    wr = r["w"] / r["n"] * 100 if r["n"] else 0
    print(f"{r['b']:<22} {r['n']:>4} {r['w']:>4} {r['l']:>4} {wr:>5.1f}% "
          f"${r['p']:>+8.2f} ${r['a']:>+7.3f}")
    total_p += r["p"]
    total_n += r["n"]
    total_w += r["w"]
print("-" * 68)
if total_n:
    print(f"{'TOTAL':<22} {total_n:>4} {total_w:>4} {total_n-total_w:>4} "
          f"{total_w/total_n*100:>5.1f}% ${total_p:>+8.2f} ${total_p/total_n:>+7.3f}")

# ---- 2. Every closed contract ----
print("\n===== ALL CLOSED CONTRACTS =====\n")
print(f"{'date':<8} {'city':<5} {'strike':<10} {'side':<3} {'qty':>3} "
      f"{'entry':>5} {'exit':>5} {'P&L':>8} {'strat':<12}")
print("-" * 76)
for r in q("""SELECT ticker, side, qty, entry_cents AS e, exit_cents AS x,
                     realized_usd AS p FROM pnl ORDER BY ticker"""):
    parts = r["ticker"].replace("KXHIGH", "").split("-")
    city = parts[0][:4] if parts else "?"
    date = parts[1][2:] if len(parts) > 1 else "?"
    strike = parts[2] if len(parts) > 2 else "?"
    side, e, x, p = r["side"], r["e"], r["x"], r["p"]
    strat = classify(side, e)
    icon = "+" if p > 0 else "-"
    print(f"{date:<8} {city:<5} {strike:<10} {side:<3} {r['qty']:>3} "
          f"{e:>4}c {x:>4}c {icon}${abs(p):>5.2f} {strat:<12}")

# ---- 3. Open positions ----
print("\n===== OPEN POSITIONS =====\n")
print(f"{'ticker':<33} {'side':<3} {'qty':>3} {'entry':>6} "
      f"{'strat':<12} {'opened':<20}")
print("-" * 82)
open_rows = q("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at DESC")
if not open_rows:
    print("  (none)")
for r in open_rows:
    side, e = r["side"], r["avg_price_cents"]
    strat = classify(side, e)
    print(f"{r['ticker']:<33} {side:<3} {r['qty']:>3} {e:>5}c "
          f"{strat:<12} {r['opened_at']:<20}")

# ---- 4. Per-city performance ----
print("\n===== PERFORMANCE BY CITY =====\n")
print(f"{'city':<6} {'n':>3} {'W':>3} {'L':>3} {'WR%':>6} {'P&L':>9}")
print("-" * 38)
city_data = {}
for r in q("SELECT ticker, realized_usd FROM pnl"):
    city = r["ticker"].replace("KXHIGH", "").split("-")[0][:4]
    if city not in city_data:
        city_data[city] = {"n": 0, "w": 0, "p": 0.0}
    city_data[city]["n"] += 1
    if r["realized_usd"] > 0:
        city_data[city]["w"] += 1
    city_data[city]["p"] += r["realized_usd"]
for city in sorted(city_data, key=lambda x: -city_data[x]["p"]):
    d = city_data[city]
    wr = d["w"] / d["n"] * 100
    print(f"{city:<6} {d['n']:>3} {d['w']:>3} {d['n']-d['w']:>3} "
          f"{wr:>5.1f}% ${d['p']:>+7.2f}")
