"""
Read-only analytics/report tool for Weather Bot v2.

Usage:
  python /app/src/report.py summary
  python /app/src/report.py today
  python /app/src/report.py positions
  python /app/src/report.py orders
  python /app/src/report.py pnl
  python /app/src/report.py events
  python /app/src/report.py export
"""
import sys, csv, os, datetime as dt
from db import conn

EXPORT_DIR = "/app/data/exports"

def rows_to_dicts(rows):
    return [dict(r) for r in rows]

def print_rows(rows, limit=20):
    rows = rows_to_dicts(rows)
    if not rows:
        print("(no rows)")
        return
    keys = list(rows[0].keys())
    widths = {}
    for k in keys:
        widths[k] = min(max(len(str(k)), *(len(str(r.get(k, ""))) for r in rows[:limit])), 36)
    print(" | ".join(k[:widths[k]].ljust(widths[k]) for k in keys))
    print("-+-".join("-" * widths[k] for k in keys))
    for r in rows[:limit]:
        print(" | ".join(str(r.get(k, ""))[:widths[k]].ljust(widths[k]) for k in keys))

def q(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).fetchall()

def table_count(table):
    try:
        return q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
    except Exception:
        return "ERR"

def summary():
    print("=== Weather Bot v2 Summary ===")
    for table in ["state", "events", "markets", "orders", "positions", "pnl"]:
        print(f"{table}: {table_count(table)}")

    print("\n=== State ===")
    print_rows(q("SELECT key, value FROM state ORDER BY key"))

    print("\n=== Open Positions ===")
    print_rows(q("""
        SELECT ticker, side, qty, avg_price_cents, status, opened_at
        FROM positions
        WHERE status='OPEN'
        ORDER BY opened_at DESC
    """))

    print("\n=== Realized PnL ===")
    rows = q("SELECT COALESCE(SUM(realized_usd), 0) AS realized_usd FROM pnl")
    total = rows[0]["realized_usd"] if rows else 0
    print(f"total_realized_usd: {total:+.2f}")

    print("\n=== Last Events ===")
    print_rows(q("""
        SELECT ts, level, module, message
        FROM events
        ORDER BY id DESC
        LIMIT 10
    """), limit=10)

def today():
    today_utc = dt.datetime.utcnow().date().isoformat()
    print(f"=== Today UTC: {today_utc} ===")

    print("\n=== Today's Orders ===")
    print_rows(q("""
        SELECT order_id, ticker, side, action, qty, price_cents, status, placed_at
        FROM orders
        WHERE substr(placed_at,1,10)=?
        ORDER BY placed_at DESC
    """, (today_utc,)))

    print("\n=== Today's PnL ===")
    rows = q("""
        SELECT COUNT(*) AS closed_trades,
               COALESCE(SUM(realized_usd), 0) AS realized_usd,
               COALESCE(AVG(realized_usd), 0) AS avg_trade_usd
        FROM pnl
        WHERE substr(closed_at,1,10)=?
    """, (today_utc,))
    print_rows(rows)

    print("\n=== Today's Events ===")
    print_rows(q("""
        SELECT ts, level, module, message
        FROM events
        WHERE substr(ts,1,10)=?
        ORDER BY id DESC
        LIMIT 20
    """, (today_utc,)), limit=20)

def positions():
    print("=== Positions ===")
    print_rows(q("""
        SELECT ticker, side, qty, avg_price_cents, tp_price, sl_price, status, opened_at
        FROM positions
        ORDER BY opened_at DESC
        LIMIT 50
    """), limit=50)

def orders():
    print("=== Orders ===")
    print_rows(q("""
        SELECT order_id, ticker, side, action, qty, price_cents, status, placed_at, filled_at
        FROM orders
        ORDER BY placed_at DESC
        LIMIT 50
    """), limit=50)

def pnl():
    print("=== PnL Summary ===")
    print_rows(q("""
        SELECT
            COUNT(*) AS trades,
            COALESCE(SUM(realized_usd), 0) AS total_usd,
            COALESCE(AVG(realized_usd), 0) AS avg_usd,
            COALESCE(MIN(realized_usd), 0) AS worst_usd,
            COALESCE(MAX(realized_usd), 0) AS best_usd
        FROM pnl
    """))

    print("\n=== Recent PnL ===")
    print_rows(q("""
        SELECT ticker, side, qty, entry_cents, exit_cents, realized_usd, closed_at, reason
        FROM pnl
        ORDER BY closed_at DESC
        LIMIT 50
    """), limit=50)

def events():
    print("=== Recent Events ===")
    print_rows(q("""
        SELECT ts, level, module, message
        FROM events
        ORDER BY id DESC
        LIMIT 50
    """), limit=50)

def export():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    tables = ["state", "events", "markets", "orders", "positions", "pnl"]
    with conn() as c:
        for table in tables:
            rows = c.execute(f"SELECT * FROM {table}").fetchall()
            path = os.path.join(EXPORT_DIR, f"{table}.csv")
            with open(path, "w", newline="") as f:
                if rows:
                    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                    writer.writeheader()
                    for r in rows:
                        writer.writerow(dict(r))
                else:
                    f.write("")
            print(f"exported {table}: {len(rows)} rows -> {path}")

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    commands = {
        "summary": summary,
        "today": today,
        "positions": positions,
        "orders": orders,
        "pnl": pnl,
        "events": events,
        "export": export,
    }
    if cmd not in commands:
        print("Unknown command:", cmd)
        print("Commands:", ", ".join(commands.keys()))
        raise SystemExit(1)
    commands[cmd]()

if __name__ == "__main__":
    main()
